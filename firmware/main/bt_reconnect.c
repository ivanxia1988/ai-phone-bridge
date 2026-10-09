/* SPDX-License-Identifier: Unlicense OR CC0-1.0 */
#include "bt_reconnect.h"

#include <stdlib.h>
#include <string.h>
#include "bt_app_core.h"
#include "esp_gap_bt_api.h"
#include "esp_timer.h"
#include "nvs.h"
#include "uart_audio.h"

/* SDK default page timeout: 0x2000 slots, each 625 microseconds. */
#define RETRY_INTERVAL_US (UINT64_C(0x2000) * 625)

typedef enum {
    RECONNECT_START = 0x7100,
    RECONNECT_TICK,
    RECONNECT_HFP,
    RECONNECT_ACL_DISCONNECTED,
    RECONNECT_AUTH,
    RECONNECT_ENABLE,
} reconnect_event_t;

typedef struct {
    esp_bd_addr_t bda;
    int value;
} reconnect_message_t;

/* All state and NVS operations run on the existing BT application task. */
static bool s_started;
static bool s_enabled = true;
static bool s_has_target;
static bool s_pending;
static bool s_had_slc;
static bool s_acl_gone;
static bool s_waiting_acl;
static bool s_saved_target;
static bool s_canceling;
static bool s_local_close_requested;
static esp_bd_addr_t s_target;
static esp_bd_addr_t s_saved;
static esp_bd_addr_t s_active;
static esp_hf_client_connection_state_t s_state = ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED;
static esp_timer_handle_t s_timer;
static int64_t s_retry_at;

static void handle_event(uint16_t event, void *param);

static bool valid_address(const uint8_t *bda)
{
    static const uint8_t zero[ESP_BD_ADDR_LEN] = {0};
    static const uint8_t broadcast[ESP_BD_ADDR_LEN] = {255, 255, 255, 255, 255, 255};
    return bda && memcmp(bda, zero, sizeof(zero)) &&
           memcmp(bda, broadcast, sizeof(broadcast));
}

static bool matches_target(const uint8_t *bda)
{
    return s_has_target && memcmp(bda, s_target, ESP_BD_ADDR_LEN) == 0;
}

/* -1 means the bond database could not be read; do not treat it as unpaired. */
static int get_bonds(esp_bd_addr_t **list)
{
    *list = NULL;
    int count = esp_bt_gap_get_bond_device_num();
    if (count <= 0) return count;
    *list = calloc((size_t)count, sizeof(esp_bd_addr_t));
    if (!*list) return -1;
    if (esp_bt_gap_get_bond_device_list(&count, *list) != ESP_OK) {
        free(*list);
        *list = NULL;
        return -1;
    }
    return count;
}

static int target_bonded(void)
{
    esp_bd_addr_t *list;
    int count = get_bonds(&list);
    int found = count < 0 ? -1 : 0;
    for (int i = 0; i < count; ++i) {
        if (matches_target(list[i])) found = 1;
    }
    free(list);
    return found;
}

static void load_target(void)
{
    nvs_handle_t handle;
    if (nvs_open("phone_bridge", NVS_READONLY, &handle) == ESP_OK) {
        size_t size = sizeof(s_saved);
        if (nvs_get_blob(handle, "last_phone", s_saved, &size) == ESP_OK &&
            size == sizeof(s_saved) && valid_address(s_saved)) {
            memcpy(s_target, s_saved, sizeof(s_target));
            s_has_target = s_saved_target = true;
        }
        nvs_close(handle);
    }
    if (!s_has_target) {
        esp_bd_addr_t *list;
        int count = get_bonds(&list);
        if (count == 1 && valid_address(list[0])) {
            memcpy(s_target, list[0], sizeof(s_target));
            s_has_target = true;
        }
        free(list);
    }
}

static void save_target(void)
{
    if (s_saved_target && memcmp(s_saved, s_target, sizeof(s_saved)) == 0) return;
    nvs_handle_t handle;
    esp_err_t err = nvs_open("phone_bridge", NVS_READWRITE, &handle);
    if (err == ESP_OK) {
        err = nvs_set_blob(handle, "last_phone", s_target, sizeof(s_target));
        if (err == ESP_OK) err = nvs_commit(handle);
        nvs_close(handle);
    }
    if (err != ESP_OK) {
        uart_audio_send_control_event("evt:bt_retry nvs_error");
        return;
    }
    memcpy(s_saved, s_target, sizeof(s_saved));
    s_saved_target = true;
}

static void pause_retry(const char *reason)
{
    s_enabled = false;
    uart_audio_send_control_event(reason);
}

static void queue_event(reconnect_event_t event, const uint8_t *bda, int value)
{
    reconnect_message_t message = {.value = value};
    if (bda) memcpy(message.bda, bda, sizeof(message.bda));
    if (!bt_app_work_dispatch(handle_event, event, &message, sizeof(message), NULL)) {
        uart_audio_send_control_event("evt:bt_retry queue_error");
    }
}

static void timer_tick(void *arg)
{
    (void)arg;
    /* Periodic dispatch retries naturally if the application queue was full. */
    (void)bt_app_work_dispatch(handle_event, RECONNECT_TICK, NULL, 0, NULL);
}

static void retry(void)
{
    if (!s_started || !s_enabled || !s_has_target || s_pending || s_waiting_acl ||
        s_state != ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED ||
        esp_timer_get_time() < s_retry_at) return;
    int bonded = target_bonded();
    if (bonded < 0) {
        uart_audio_send_control_event("evt:bt_retry bond_read_error");
        return;
    }
    if (!bonded) {
        pause_retry("evt:bt_retry paused=unpaired");
        return;
    }
    s_retry_at = esp_timer_get_time() + RETRY_INTERVAL_US;
    esp_err_t err = esp_hf_client_connect(s_target);
    if (err == ESP_OK) {
        s_pending = true;
        s_acl_gone = false;
        uart_audio_send_control_event("evt:bt_retry connecting");
    } else {
        uart_audio_send_control_event("evt:bt_retry connect_error");
    }
}

static void handle_event(uint16_t event, void *param)
{
    const reconnect_message_t *message = param;
    switch (event) {
    case RECONNECT_START: {
        if (s_started) break;
        load_target();
        const esp_timer_create_args_t args = {
            .callback = timer_tick,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "bt_reconnect",
            .skip_unhandled_events = true,
        };
        if (esp_timer_create(&args, &s_timer) != ESP_OK ||
            esp_timer_start_periodic(s_timer, RETRY_INTERVAL_US) != ESP_OK) {
            pause_retry("evt:bt_retry timer_error");
            break;
        }
        s_started = true;
        s_retry_at = esp_timer_get_time() + RETRY_INTERVAL_US;
        break;
    }
    case RECONNECT_TICK:
        retry();
        break;
    case RECONNECT_HFP:
        if (message->value == ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED &&
            s_state != ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED &&
            memcmp(message->bda, s_active, sizeof(s_active)) != 0) break;
        s_state = (esp_hf_client_connection_state_t)message->value;
        if (s_state != ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED) {
            memcpy(s_active, message->bda, sizeof(s_active));
        }
        if (s_state == ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED && valid_address(message->bda)) {
            /* A connect may finish after the explicit cancellation was queued. */
            if (s_canceling) {
                if (esp_hf_client_disconnect(s_active) != ESP_OK) {
                    uart_audio_send_control_event("evt:bt_retry disconnect_error");
                }
                break;
            }
            memcpy(s_target, message->bda, sizeof(s_target));
            s_has_target = true;
            s_enabled = true;
            s_pending = s_waiting_acl = s_acl_gone = false;
            s_had_slc = true;
            save_target();
        } else if (s_state == ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED) {
            s_pending = false;
            s_canceling = false;
            /* HFP has no reason field. Wait for ACL reason before deciding
             * whether a previously established link should be reconnected. */
            s_waiting_acl = s_had_slc && !s_acl_gone;
            if (!s_waiting_acl) s_had_slc = false;
            s_retry_at = esp_timer_get_time() + RETRY_INTERVAL_US;
        }
        break;
    case RECONNECT_ACL_DISCONNECTED:
        if (!matches_target(message->bda)) break;
        s_acl_gone = true;
        if (s_had_slc && (message->value == ESP_BT_STATUS_HCI_PEER_USER ||
                         (message->value == ESP_BT_STATUS_HCI_CONN_CAUSE_LOCAL_HOST &&
                          !(s_local_close_requested && s_enabled)))) {
            pause_retry("evt:bt_retry paused=manual_disconnect");
        } else if (message->value == ESP_BT_STATUS_HCI_AUTH_FAILURE ||
                   message->value == ESP_BT_STATUS_HCI_KEY_MISSING) {
            pause_retry("evt:bt_retry paused=authentication");
        }
        s_local_close_requested = false;
        if (s_state == ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED) {
            s_pending = s_waiting_acl = s_had_slc = false;
            s_retry_at = esp_timer_get_time() + RETRY_INTERVAL_US;
        }
        break;
    case RECONNECT_AUTH:
        if (matches_target(message->bda) && message->value != ESP_BT_STATUS_SUCCESS) {
            pause_retry("evt:bt_retry paused=authentication");
        }
        break;
    case RECONNECT_ENABLE:
        s_enabled = message->value != 0;
        if (s_enabled) {
            s_canceling = false;
            s_waiting_acl = false;
            s_retry_at = esp_timer_get_time();
        } else {
            s_canceling = s_pending || s_state == ESP_HF_CLIENT_CONNECTION_STATE_CONNECTING ||
                          s_state == ESP_HF_CLIENT_CONNECTION_STATE_CONNECTED;
            if (s_state != ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED || s_pending) {
                /* The corresponding ACL close may arrive after a later resume
                 * command; it must not overwrite that newer local intent. */
                s_local_close_requested = true;
                uint8_t *bda = s_state == ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED ? s_target : s_active;
                if (esp_hf_client_disconnect(bda) != ESP_OK) {
                    uart_audio_send_control_event("evt:bt_retry disconnect_error");
                }
            }
            uart_audio_send_control_event("evt:bt_retry paused=command");
        }
        break;
    default:
        break;
    }
}

void bt_reconnect_start(void) { queue_event(RECONNECT_START, NULL, 0); }
void bt_reconnect_hfp_event(esp_hf_client_connection_state_t state, const uint8_t *bda)
{ queue_event(RECONNECT_HFP, bda, state); }
void bt_reconnect_acl_disconnected(const uint8_t *bda, esp_bt_status_t reason)
{ queue_event(RECONNECT_ACL_DISCONNECTED, bda, reason); }
void bt_reconnect_auth_event(const uint8_t *bda, esp_bt_status_t status)
{ queue_event(RECONNECT_AUTH, bda, status); }
void bt_reconnect_set_enabled(bool enabled)
{ queue_event(RECONNECT_ENABLE, NULL, enabled); }
bool bt_reconnect_enabled(void) { return s_started && s_enabled; }
bool bt_reconnect_pending(void) { return s_pending; }
