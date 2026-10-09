/*
 * SPDX-FileCopyrightText: 2021-2022 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Unlicense OR CC0-1.0
 */

#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#include <ctype.h>
#include <stdio.h>
#include "esp_log.h"

#include "bt_app_core.h"
#include "bt_app_hf.h"
#include "bt_reconnect.h"
#include "esp_bt_main.h"
#include "esp_bt_device.h"
#include "esp_gap_bt_api.h"
#include "esp_hf_client_api.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/ringbuf.h"
#include "time.h"
#include "sys/time.h"
#include "sdkconfig.h"
#include "uart_audio.h"
#include "i2s_output.h"

const char *c_hf_evt_str[] = {
    "CONNECTION_STATE_EVT",              /*!< connection state changed event */
    "AUDIO_STATE_EVT",                   /*!< audio connection state change event */
    "VR_STATE_CHANGE_EVT",                /*!< voice recognition state changed */
    "CALL_IND_EVT",                      /*!< call indication event */
    "CALL_SETUP_IND_EVT",                /*!< call setup indication event */
    "CALL_HELD_IND_EVT",                 /*!< call held indicator event */
    "NETWORK_STATE_EVT",                 /*!< network state change event */
    "SIGNAL_STRENGTH_IND_EVT",           /*!< signal strength indication event */
    "ROAMING_STATUS_IND_EVT",            /*!< roaming status indication event */
    "BATTERY_LEVEL_IND_EVT",             /*!< battery level indication event */
    "CURRENT_OPERATOR_EVT",              /*!< current operator name event */
    "RESP_AND_HOLD_EVT",                 /*!< response and hold event */
    "CLIP_EVT",                          /*!< Calling Line Identification notification event */
    "CALL_WAITING_EVT",                  /*!< call waiting notification */
    "CLCC_EVT",                          /*!< listing current calls event */
    "VOLUME_CONTROL_EVT",                /*!< audio volume control event */
    "AT_RESPONSE",                       /*!< audio volume control event */
    "SUBSCRIBER_INFO_EVT",               /*!< subscriber information event */
    "INBAND_RING_TONE_EVT",              /*!< in-band ring tone settings */
    "LAST_VOICE_TAG_NUMBER_EVT",         /*!< requested number from AG event */
    "RING_IND_EVT",                      /*!< ring indication event */
};

// esp_hf_client_connection_state_t
const char *c_connection_state_str[] = {
    "disconnected",
    "connecting",
    "connected",
    "slc_connected",
    "disconnecting",
};

// esp_hf_client_audio_state_t
const char *c_audio_state_str[] = {
    "disconnected",
    "connecting",
    "connected",
    "connected_msbc",
};

/// esp_hf_vr_state_t
const char *c_vr_state_str[] = {
    "disabled",
    "enabled",
};

// esp_hf_service_availability_status_t
const char *c_service_availability_status_str[] = {
    "unavailable",
    "available",
};

// esp_hf_roaming_status_t
const char *c_roaming_status_str[] = {
    "inactive",
    "active",
};

// esp_hf_client_call_state_t
const char *c_call_str[] = {
    "NO call in progress",
    "call in progress",
};

// esp_hf_client_callsetup_t
const char *c_call_setup_str[] = {
    "NONE",
    "INCOMING",
    "OUTGOING_DIALING",
    "OUTGOING_ALERTING"
};

// esp_hf_client_callheld_t
const char *c_call_held_str[] = {
    "NONE held",
    "Held and Active",
    "Held",
};

// esp_hf_response_and_hold_status_t
const char *c_resp_and_hold_str[] = {
    "HELD",
    "HELD ACCEPTED",
    "HELD REJECTED",
};

// esp_hf_client_call_direction_t
const char *c_call_dir_str[] = {
    "outgoing",
    "incoming",
};

// esp_hf_client_call_state_t
const char *c_call_state_str[] = {
    "active",
    "held",
    "dialing",
    "alerting",
    "incoming",
    "waiting",
    "held_by_resp_hold",
};

// esp_hf_current_call_mpty_type_t
const char *c_call_mpty_type_str[] = {
    "single",
    "multi",
};

// esp_hf_volume_control_target_t
const char *c_volume_control_target_str[] = {
    "SPEAKER",
    "MICROPHONE"
};

// esp_hf_at_response_code_t
const char *c_at_response_code_str[] = {
    "OK",
    "ERROR",
    "ERR_NO_CARRIER",
    "ERR_BUSY",
    "ERR_NO_ANSWER",
    "ERR_DELAYED",
    "ERR_BLACKLILSTED",
    "ERR_CME",
};

// esp_hf_subscriber_service_type_t
const char *c_subscriber_service_type_str[] = {
    "unknown",
    "voice",
    "fax",
};

// esp_hf_client_in_band_ring_state_t
const char *c_inband_ring_state_str[] = {
    "NOT provided",
    "Provided",
};

extern esp_bd_addr_t peer_addr;
// If you want to connect a specific device, add it's address here
// esp_bd_addr_t peer_addr = {0xac, 0x67, 0xb2, 0x53, 0x77, 0xbe};

typedef enum {
    HF_UART_CTRL_DIAL = 10,
    HF_UART_CTRL_FLUSH,
    HF_UART_CTRL_HANGUP = 1,
    HF_UART_CTRL_DTMF,
    HF_UART_CTRL_AUDIO_ON,
    HF_UART_CTRL_AUDIO_OFF,
    HF_UART_CTRL_QUERY_CALL,
    HF_UART_CTRL_BT_DISCONNECT = 12,
    HF_UART_CTRL_BT_RECONNECT,
} hf_uart_ctrl_opcode_t;

typedef struct {
    uint8_t opcode;
    uint8_t arg;
    char number[32];
} hf_uart_ctrl_cmd_t;

enum {
    BT_APP_HF_UART_CTRL_EVT = 0x7000,
};

static esp_hf_client_connection_state_t s_conn_state = ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED;
static esp_hf_client_audio_state_t s_audio_state = ESP_HF_CLIENT_AUDIO_STATE_DISCONNECTED;
static esp_hf_call_status_t s_call_status = ESP_HF_CALL_STATUS_NO_CALLS;
static esp_hf_call_setup_status_t s_call_setup_status = ESP_HF_CALL_SETUP_STATUS_IDLE;
static esp_hf_call_held_status_t s_call_held_status = ESP_HF_CALL_HELD_STATUS_NONE;

static bool bt_app_hf_has_active_or_setup_call(void)
{
    return (s_call_status == ESP_HF_CALL_STATUS_CALL_IN_PROGRESS) ||
           (s_call_setup_status != ESP_HF_CALL_SETUP_STATUS_IDLE);
}

static bool bt_app_hf_is_valid_dtmf(char c)
{
    if (c >= '0' && c <= '9') {
        return true;
    }
    if (c == '*' || c == '#') {
        return true;
    }
    if (c >= 'A' && c <= 'D') {
        return true;
    }
    return false;
}

static void bt_app_hf_uart_ctrl_evt_handler(uint16_t event, void *param)
{
    if (event != BT_APP_HF_UART_CTRL_EVT || !param) {
        return;
    }

    hf_uart_ctrl_cmd_t *cmd = (hf_uart_ctrl_cmd_t *)param;
    switch (cmd->opcode) {
        case HF_UART_CTRL_DIAL: {
            if (s_conn_state != ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED) {
                uart_audio_send_control_event("resp:dial not_connected");
            } else if (bt_app_hf_has_active_or_setup_call()) {
                uart_audio_send_control_event("resp:dial busy");
            } else {
                esp_err_t err = esp_hf_client_dial(cmd->number);
                uart_audio_send_control_event(err == ESP_OK ? "resp:dial queued" : "resp:dial error");
            }
            break;
        }
        case HF_UART_CTRL_FLUSH:
            uart_audio_flush_uplink();
            uart_audio_send_control_event("resp:flush ok");
            break;
        case HF_UART_CTRL_HANGUP:
            ESP_LOGI(BT_HF_TAG, "UART CTRL: hangup");
            if (s_conn_state != ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED) {
                uart_audio_send_control_event("resp:hangup not_connected");
            } else {
                esp_err_t err = esp_hf_client_reject_call();
                uart_audio_send_control_event(err == ESP_OK ? "resp:hangup queued" : "resp:hangup error");
            }
            break;
        case HF_UART_CTRL_DTMF:
            ESP_LOGI(BT_HF_TAG, "UART CTRL: dtmf %c", (char)cmd->arg);
            if (!bt_app_hf_has_active_or_setup_call()) {
                uart_audio_send_control_event("resp:dtmf no_call");
            } else {
                (void)esp_hf_client_send_dtmf((char)cmd->arg);
                uart_audio_send_control_event("resp:dtmf ok");
            }
            break;
        case HF_UART_CTRL_AUDIO_ON:
            ESP_LOGI(BT_HF_TAG, "UART CTRL: audio_on");
            (void)esp_hf_client_connect_audio(peer_addr);
            uart_audio_send_control_event("resp:audio_on ok");
            break;
        case HF_UART_CTRL_AUDIO_OFF:
            ESP_LOGI(BT_HF_TAG, "UART CTRL: audio_off");
            (void)esp_hf_client_disconnect_audio(peer_addr);
            uart_audio_send_control_event("resp:audio_off ok");
            break;
        case HF_UART_CTRL_QUERY_CALL:
        {
            char status[192];
            const char *summary = bt_app_hf_has_active_or_setup_call() ? "in_call" : "no_call";
            int n = snprintf(status,
                             sizeof(status),
                             "state:conn=%d audio=%d call=%d setup=%d held=%d rate=%d firmware=1 bt_build=2 bt_auto=%d bt_retry=%d summary=%s",
                             (int)s_conn_state,
                             (int)s_audio_state,
                             (int)s_call_status,
                             (int)s_call_setup_status,
                             (int)s_call_held_status,
                             s_audio_state == ESP_HF_CLIENT_AUDIO_STATE_CONNECTED_MSBC ? 16000 : 8000,
                             (int)bt_reconnect_enabled(),
                             (int)bt_reconnect_pending(),
                             summary);
            if (n > 0) {
                uart_audio_send_control_event(status);
            }
            break;
        }
        case HF_UART_CTRL_BT_DISCONNECT:
        case HF_UART_CTRL_BT_RECONNECT:
            bt_reconnect_set_enabled(cmd->opcode == HF_UART_CTRL_BT_RECONNECT);
            break;
        default:
            ESP_LOGW(BT_HF_TAG, "UART CTRL: unknown opcode=%u", (unsigned)cmd->opcode);
            break;
    }
}

bool bt_app_hf_handle_uart_control(const uint8_t *payload, uint16_t len)
{
    if (!payload || len == 0) {
        return false;
    }

    char cmd_text[64];
    size_t n = len;
    if (n >= sizeof(cmd_text)) {
        return false;
    }
    if (memchr(payload, 0, n)) return false;
    memcpy(cmd_text, payload, n);
    cmd_text[n] = '\0';

    hf_uart_ctrl_cmd_t cmd = {0};

    if (strncmp(cmd_text, "dial:", 5) == 0) {
        const char *number = cmd_text + 5;
        size_t count = strlen(number);
        if (count < 3 || count >= sizeof(cmd.number)) return false;
        size_t start = number[0] == '+' ? 1 : 0;
        for (size_t i = start; i < count; ++i) {
            if (number[i] < '0' || number[i] > '9') return false;
        }
        memcpy(cmd.number, number, count + 1);
        cmd.opcode = HF_UART_CTRL_DIAL;
    } else if (strcmp(cmd_text, "flush") == 0) {
        cmd.opcode = HF_UART_CTRL_FLUSH;
    } else if (strcmp(cmd_text, "hangup") == 0 || strcmp(cmd_text, "hu") == 0) {
        cmd.opcode = HF_UART_CTRL_HANGUP;
    } else if (strcmp(cmd_text, "audio_on") == 0) {
        cmd.opcode = HF_UART_CTRL_AUDIO_ON;
    } else if (strcmp(cmd_text, "audio_off") == 0) {
        cmd.opcode = HF_UART_CTRL_AUDIO_OFF;
    } else if (strcmp(cmd_text, "bt_disconnect") == 0) {
        cmd.opcode = HF_UART_CTRL_BT_DISCONNECT;
    } else if (strcmp(cmd_text, "bt_reconnect") == 0) {
        cmd.opcode = HF_UART_CTRL_BT_RECONNECT;
    } else if (strcmp(cmd_text, "query_call") == 0 || strcmp(cmd_text, "status") == 0 || strcmp(cmd_text, "qs") == 0) {
        cmd.opcode = HF_UART_CTRL_QUERY_CALL;
    } else if (strncmp(cmd_text, "dtmf:", 5) == 0 && cmd_text[5] != '\0' && cmd_text[6] == '\0') {
        char tone = cmd_text[5];
        if (tone >= 'a' && tone <= 'd') {
            tone = (char)toupper((int)tone);
        }
        if (!bt_app_hf_is_valid_dtmf(tone)) {
            ESP_LOGW(BT_HF_TAG, "UART CTRL invalid dtmf: %s", cmd_text);
            return false;
        }
        cmd.opcode = HF_UART_CTRL_DTMF;
        cmd.arg = (uint8_t)tone;
    } else {
        ESP_LOGW(BT_HF_TAG, "UART CTRL unsupported: %s", cmd_text);
        return false;
    }

    return bt_app_work_dispatch(bt_app_hf_uart_ctrl_evt_handler,
                                BT_APP_HF_UART_CTRL_EVT,
                                &cmd,
                                sizeof(cmd),
                                NULL);
}

#if CONFIG_BT_HFP_AUDIO_DATA_PATH_HCI

static uint32_t bt_app_hf_client_outgoing_cb(uint8_t *p_buf, uint32_t sz)
{
    if (!uart_audio_try_get_uplink(p_buf, sz)) {
        memset(p_buf, 0, sz);
    }
    return sz;
}

static void bt_app_hf_client_incoming_cb(const uint8_t *buf, uint32_t sz)
{
    uart_audio_send_downlink(buf, sz);
}
#endif /* #if CONFIG_BT_HFP_AUDIO_DATA_PATH_HCI */

/* callback for HF_CLIENT */
void bt_app_hf_client_cb(esp_hf_client_cb_event_t event, esp_hf_client_cb_param_t *param)
{
    if (event <= ESP_HF_CLIENT_RING_IND_EVT) {
        ESP_LOGI(BT_HF_TAG, "APP HFP event: %s", c_hf_evt_str[event]);
    } else {
        ESP_LOGE(BT_HF_TAG, "APP HFP invalid event %d", event);
    }

    switch (event) {
        case ESP_HF_CLIENT_PROF_STATE_EVT:
            if (param->prof_stat.state == ESP_HF_INIT_SUCCESS) {
                bt_reconnect_start();
            }
            break;
        case ESP_HF_CLIENT_CONNECTION_STATE_EVT:
        {
            s_conn_state = param->conn_stat.state;
            ESP_LOGI(BT_HF_TAG, "--connection state %s, peer feats 0x%"PRIx32", chld_feats 0x%"PRIx32,
                    c_connection_state_str[param->conn_stat.state],
                    param->conn_stat.peer_feat,
                    param->conn_stat.chld_feat);
            memcpy(peer_addr,param->conn_stat.remote_bda,ESP_BD_ADDR_LEN);
            bt_reconnect_hfp_event(param->conn_stat.state, param->conn_stat.remote_bda);

#if CONFIG_BT_HFP_AUDIO_DATA_PATH_HCI
            if (param->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED) {
                uart_audio_send_control_event("evt:slc_connected");
            } else if (param->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED) {
                uart_audio_send_control_event("evt:disconnected");
            }
#endif

            if (param->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED) {
                // Best-effort: ensure AG doesn't keep mic gain at 0.
                (void)esp_hf_client_volume_update(ESP_HF_VOLUME_CONTROL_TARGET_MIC, 15);
                (void)esp_hf_client_volume_update(ESP_HF_VOLUME_CONTROL_TARGET_SPK, 15);
            }
            break;
        }

        case ESP_HF_CLIENT_AUDIO_STATE_EVT:
        {
            s_audio_state = param->audio_stat.state;
            ESP_LOGI(BT_HF_TAG, "--audio state %s",
                    c_audio_state_str[param->audio_stat.state]);
    #if CONFIG_BT_HFP_AUDIO_DATA_PATH_HCI
            const bool audio_connected =
                (param->audio_stat.state == ESP_HF_CLIENT_AUDIO_STATE_CONNECTED) ||
                (param->audio_stat.state == ESP_HF_CLIENT_AUDIO_STATE_CONNECTED_MSBC);

            if (audio_connected) {
#if !I2S_OUTPUT_ALWAYS_ON
                /* mSBC = 16 kHz, CVSD = 8 kHz */
                int i2s_rate = (param->audio_stat.state == ESP_HF_CLIENT_AUDIO_STATE_CONNECTED_MSBC)
                               ? 16000 : 8000;
                i2s_output_init(i2s_rate);
#endif
                uart_audio_init();
                uart_audio_set_audio_connected(true);
                esp_hf_client_register_data_callback(bt_app_hf_client_incoming_cb,
                                                    bt_app_hf_client_outgoing_cb);
                uart_audio_send_control_event(param->audio_stat.state == ESP_HF_CLIENT_AUDIO_STATE_CONNECTED_MSBC ? "evt:audio_connected rate=16000" : "evt:audio_connected rate=8000");
            } else {
                // Covers DISCONNECTED and any transient states (e.g. disconnecting),
                // so we won't poke the BT stack when air mode is no longer valid.
                uart_audio_set_audio_connected(false);
                uart_audio_flush_uplink();
#if !I2S_OUTPUT_ALWAYS_ON
                i2s_output_deinit();
#endif
                uart_audio_send_control_event("evt:audio_disconnected");
            }
    #endif /* #if CONFIG_BT_HFP_AUDIO_DATA_PATH_HCI */
            break;
        }

        case ESP_HF_CLIENT_BVRA_EVT:
        {
            ESP_LOGI(BT_HF_TAG, "--VR state %s",
                    c_vr_state_str[param->bvra.value]);
            break;
        }

        case ESP_HF_CLIENT_CIND_SERVICE_AVAILABILITY_EVT:
        {
            ESP_LOGI(BT_HF_TAG, "--NETWORK STATE %s",
                    c_service_availability_status_str[param->service_availability.status]);
            break;
        }

        case ESP_HF_CLIENT_CIND_ROAMING_STATUS_EVT:
        {
            ESP_LOGI(BT_HF_TAG, "--ROAMING: %s",
                    c_roaming_status_str[param->roaming.status]);
            break;
        }

        case ESP_HF_CLIENT_CIND_SIGNAL_STRENGTH_EVT:
        {
            ESP_LOGI(BT_HF_TAG, "-- signal strength: %d",
                    param->signal_strength.value);
            break;
        }

        case ESP_HF_CLIENT_CIND_BATTERY_LEVEL_EVT:
        {
            ESP_LOGI(BT_HF_TAG, "--battery level %d",
                    param->battery_level.value);
            break;
        }

        case ESP_HF_CLIENT_COPS_CURRENT_OPERATOR_EVT:
        {
            ESP_LOGI(BT_HF_TAG, "--operator name: %s",
                    param->cops.name);
            break;
        }

        case ESP_HF_CLIENT_CIND_CALL_EVT:
        {
            s_call_status = param->call.status;
            ESP_LOGI(BT_HF_TAG, "--Call indicator %s",
                    c_call_str[param->call.status]);
#if CONFIG_BT_HFP_AUDIO_DATA_PATH_HCI
            if (param->call.status == ESP_HF_CALL_STATUS_CALL_IN_PROGRESS) {
                uart_audio_send_control_event("evt:call_active");
            } else {
                uart_audio_send_control_event("evt:call_idle");
            }
#endif
            break;
        }

        case ESP_HF_CLIENT_CIND_CALL_SETUP_EVT:
        {
            s_call_setup_status = param->call_setup.status;
            char setup[40];
            snprintf(setup, sizeof(setup), "evt:setup=%d", (int)s_call_setup_status);
            uart_audio_send_control_event(setup);
            ESP_LOGI(BT_HF_TAG, "--Call setup indicator %s",
                    c_call_setup_str[param->call_setup.status]);
            break;
        }

        case ESP_HF_CLIENT_CIND_CALL_HELD_EVT:
        {
            s_call_held_status = param->call_held.status;
            ESP_LOGI(BT_HF_TAG, "--Call held indicator %s",
                    c_call_held_str[param->call_held.status]);
            break;
        }

        case ESP_HF_CLIENT_BTRH_EVT:
        {
            ESP_LOGI(BT_HF_TAG, "--response and hold %s",
                    c_resp_and_hold_str[param->btrh.status]);
            break;
        }

        case ESP_HF_CLIENT_CLIP_EVT:
        {
            ESP_LOGI(BT_HF_TAG, "--clip number %s",
                    (param->clip.number == NULL) ? "NULL" : (param->clip.number));
            break;
        }

        case ESP_HF_CLIENT_CCWA_EVT:
        {
            ESP_LOGI(BT_HF_TAG, "--call_waiting %s",
                    (param->ccwa.number == NULL) ? "NULL" : (param->ccwa.number));
            break;
        }

        case ESP_HF_CLIENT_CLCC_EVT:
        {
            ESP_LOGI(BT_HF_TAG, "--Current call: idx %d, dir %s, state %s, mpty %s, number %s",
                    param->clcc.idx,
                    c_call_dir_str[param->clcc.dir],
                    c_call_state_str[param->clcc.status],
                    c_call_mpty_type_str[param->clcc.mpty],
                    (param->clcc.number == NULL) ? "NULL" : (param->clcc.number));
            break;
        }

        case ESP_HF_CLIENT_VOLUME_CONTROL_EVT:
        {
            ESP_LOGI(BT_HF_TAG, "--volume_target: %s, volume %d",
                    c_volume_control_target_str[param->volume_control.type],
                    param->volume_control.volume);
            break;
        }

        case ESP_HF_CLIENT_AT_RESPONSE_EVT:
        {
            char at[40];
            snprintf(at, sizeof(at), "evt:at code=%d", (int)param->at_response.code);
            uart_audio_send_control_event(at);
            ESP_LOGI(BT_HF_TAG, "--AT response event, code %d, cme %d",
                    param->at_response.code, param->at_response.cme);
            break;
        }

        case ESP_HF_CLIENT_CNUM_EVT:
        {
            ESP_LOGI(BT_HF_TAG, "--subscriber type %s, number %s",
                    c_subscriber_service_type_str[param->cnum.type],
                    (param->cnum.number == NULL) ? "NULL" : param->cnum.number);
            break;
        }

        case ESP_HF_CLIENT_BSIR_EVT:
        {
            ESP_LOGI(BT_HF_TAG, "--inband ring state %s",
                    c_inband_ring_state_str[param->bsir.state]);
            break;
        }

        case ESP_HF_CLIENT_BINP_EVT:
        {
            ESP_LOGI(BT_HF_TAG, "--last voice tag number: %s",
                    (param->binp.number == NULL) ? "NULL" : param->binp.number);
            break;
        }

        default:
            ESP_LOGE(BT_HF_TAG, "HF_CLIENT EVT: %d", event);
            break;
    }
}
