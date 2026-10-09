"""Execute firmware C with simulated SDK boundaries; no RF/hardware claims."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]

STUBS = r"""
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_NVS_NOT_FOUND 0x1102
#define ESP_BD_ADDR_LEN 6
typedef uint8_t esp_bd_addr_t[ESP_BD_ADDR_LEN];
typedef enum {
    ESP_BT_STATUS_SUCCESS = 0,
    ESP_BT_STATUS_HCI_AUTH_FAILURE = 0x105,
    ESP_BT_STATUS_HCI_KEY_MISSING = 0x106,
    ESP_BT_STATUS_HCI_CONNECTION_TOUT = 0x108,
    ESP_BT_STATUS_HCI_PEER_USER = 0x113,
    ESP_BT_STATUS_HCI_PEER_POWER_OFF = 0x115,
    ESP_BT_STATUS_HCI_CONN_CAUSE_LOCAL_HOST = 0x116
} esp_bt_status_t;
typedef enum {
    ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED,
    ESP_HF_CLIENT_CONNECTION_STATE_CONNECTING,
    ESP_HF_CLIENT_CONNECTION_STATE_CONNECTED,
    ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED,
    ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTING
} esp_hf_client_connection_state_t;
esp_err_t esp_hf_client_connect(esp_bd_addr_t bda);
esp_err_t esp_hf_client_disconnect(esp_bd_addr_t bda);
int esp_bt_gap_get_bond_device_num(void);
esp_err_t esp_bt_gap_get_bond_device_list(int *num, esp_bd_addr_t *list);
typedef unsigned nvs_handle_t;
#define NVS_READONLY 0
#define NVS_READWRITE 1
esp_err_t nvs_open(const char *, int, nvs_handle_t *);
esp_err_t nvs_get_blob(nvs_handle_t, const char *, void *, size_t *);
esp_err_t nvs_set_blob(nvs_handle_t, const char *, const void *, size_t);
esp_err_t nvs_commit(nvs_handle_t);
void nvs_close(nvs_handle_t);
typedef void (*esp_timer_cb_t)(void *);
typedef void *esp_timer_handle_t;
typedef struct {
    esp_timer_cb_t callback;
    void *arg;
    int dispatch_method;
    const char *name;
    bool skip_unhandled_events;
} esp_timer_create_args_t;
#define ESP_TIMER_TASK 0
esp_err_t esp_timer_create(const esp_timer_create_args_t *, esp_timer_handle_t *);
esp_err_t esp_timer_start_periodic(esp_timer_handle_t, uint64_t);
int64_t esp_timer_get_time(void);
void uart_audio_send_control_event(const char *);
"""

HARNESS = r"""
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sdk_stubs.h"
#include "bt_app_core.h"
#include "bt_reconnect.h"

static const esp_bd_addr_t phone = {1, 2, 3, 4, 5, 6};
static const esp_bd_addr_t other = {6, 5, 4, 3, 2, 1};
static esp_bd_addr_t bonds[2], saved, dialed, disconnected_peer;
static int bond_count, connects, disconnects, writes, commits;
static bool has_saved;
static esp_err_t connect_result, timer_result, nvs_result, list_result;
static int64_t now;
static uint64_t period;
static esp_timer_cb_t tick;
static void *tick_arg;
static char last_event[128];
typedef struct { bt_app_cb_t callback; uint16_t event; void *param; } message_t;
static message_t queue[128];
static size_t read_index, write_index;

bool bt_app_work_dispatch(bt_app_cb_t cb, uint16_t event, void *param,
                          int len, bt_app_copy_cb_t copy) {
    (void)copy;
    assert(write_index < 128);
    void *owned = NULL;
    if (len) { owned = malloc((size_t)len); assert(owned); memcpy(owned, param, (size_t)len); }
    queue[write_index++] = (message_t){cb, event, owned};
    return true;
}
static void drain(void) {
    while (read_index < write_index) {
        message_t msg = queue[read_index++];
        msg.callback(msg.event, msg.param);
        free(msg.param);
    }
    read_index = write_index = 0;
}
esp_err_t esp_hf_client_connect(esp_bd_addr_t bda) {
    connects++; memcpy(dialed, bda, 6); return connect_result;
}
esp_err_t esp_hf_client_disconnect(esp_bd_addr_t bda) {
    memcpy(disconnected_peer, bda, 6); disconnects++; return ESP_OK;
}
int esp_bt_gap_get_bond_device_num(void) { return bond_count; }
esp_err_t esp_bt_gap_get_bond_device_list(int *num, esp_bd_addr_t *list) {
    if (list_result != ESP_OK) return list_result;
    assert(*num >= bond_count);
    memcpy(list, bonds, (size_t)bond_count * 6); *num = bond_count; return ESP_OK;
}
esp_err_t nvs_open(const char *name, int mode, nvs_handle_t *h) {
    (void)name; (void)mode; *h = 1; return nvs_result;
}
esp_err_t nvs_get_blob(nvs_handle_t h, const char *key, void *blob, size_t *len) {
    (void)h; (void)key;
    if (!has_saved) return ESP_ERR_NVS_NOT_FOUND;
    assert(*len >= 6); memcpy(blob, saved, 6); *len = 6; return ESP_OK;
}
esp_err_t nvs_set_blob(nvs_handle_t h, const char *key, const void *blob, size_t len) {
    (void)h; (void)key; assert(len == 6); memcpy(saved, blob, 6); has_saved = true; writes++; return ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t h) { (void)h; commits++; return ESP_OK; }
void nvs_close(nvs_handle_t h) { (void)h; }
esp_err_t esp_timer_create(const esp_timer_create_args_t *args, esp_timer_handle_t *h) {
    tick = args->callback; tick_arg = args->arg; *h = (void *)1; return timer_result;
}
esp_err_t esp_timer_start_periodic(esp_timer_handle_t h, uint64_t interval) {
    (void)h; period = interval; return timer_result;
}
int64_t esp_timer_get_time(void) { return now; }
void uart_audio_send_control_event(const char *event) {
    snprintf(last_event, sizeof(last_event), "%s", event);
}
static void advance(void) {
    now += (int64_t)period; if (tick) tick(tick_arg); drain();
}
static void state(esp_hf_client_connection_state_t value, const uint8_t *addr) {
    bt_reconnect_hfp_event(value, addr); drain();
}
static void acl(esp_bt_status_t reason, const uint8_t *addr) {
    bt_reconnect_acl_disconnected(addr, reason); drain();
}
static void start(int count) {
    bond_count = count; memcpy(bonds[0], phone, 6); memcpy(bonds[1], other, 6);
    bt_reconnect_start(); drain();
}
static void linked(void) {
    start(1); state(ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED, phone);
}

int main(int argc, char **argv) {
    assert(argc == 2);
    const char *name = argv[1];
    if (!strcmp(name, "no_bonds")) {
        start(0); advance(); assert(connects == 0);
    } else if (!strcmp(name, "single_bond_start")) {
        start(1); advance(); assert(connects == 1 && !memcmp(dialed, phone, 6));
    } else if (!strcmp(name, "multiple_bonds")) {
        start(2); advance(); assert(connects == 0);
    } else if (!strcmp(name, "saved_peer")) {
        has_saved = true; memcpy(saved, other, 6); start(2); advance();
        assert(connects == 1 && !memcmp(dialed, other, 6));
    } else if (!strcmp(name, "stale_saved_peer")) {
        has_saved = true; memcpy(saved, other, 6); start(1); advance(); assert(connects == 0);
    } else if (!strcmp(name, "save_on_success")) {
        linked(); assert(writes == 1 && commits == 1 && !memcmp(saved, phone, 6));
        state(ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED, phone); assert(writes == 1);
        advance(); assert(connects == 0);
    } else if (!strcmp(name, "range_hfp_first")) {
        linked(); state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone);
        advance(); assert(connects == 0);
        acl(ESP_BT_STATUS_HCI_CONNECTION_TOUT, phone); advance(); assert(connects == 1);
        state(ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED, phone); advance(); assert(connects == 1);
    } else if (!strcmp(name, "range_acl_first")) {
        linked(); acl(ESP_BT_STATUS_HCI_CONNECTION_TOUT, phone); advance(); assert(connects == 0);
        state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone); advance(); assert(connects == 1);
    } else if (!strcmp(name, "one_request")) {
        start(1); advance(); advance(); advance(); assert(connects == 1);
        state(ESP_HF_CLIENT_CONNECTION_STATE_CONNECTING, phone); advance(); assert(connects == 1);
    } else if (!strcmp(name, "retry_failure")) {
        start(1); advance(); state(ESP_HF_CLIENT_CONNECTION_STATE_CONNECTING, phone);
        state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone); advance(); assert(connects == 2);
    } else if (!strcmp(name, "retry_without_connecting_callback")) {
        start(1); advance();
        state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone); advance(); assert(connects == 2);
    } else if (!strcmp(name, "immediate_failure")) {
        connect_result = ESP_FAIL; start(1); advance(); advance(); assert(connects == 2);
    } else if (!strcmp(name, "manual_remote") || !strcmp(name, "manual_local")) {
        linked(); state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone);
        acl(!strcmp(name, "manual_remote") ? ESP_BT_STATUS_HCI_PEER_USER :
            ESP_BT_STATUS_HCI_CONN_CAUSE_LOCAL_HOST, phone);
        advance(); advance(); assert(connects == 0 && !bt_reconnect_enabled());
        state(ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED, phone);
        assert(bt_reconnect_enabled());
    } else if (!strcmp(name, "manual_acl_first")) {
        linked(); acl(ESP_BT_STATUS_HCI_PEER_USER, phone);
        state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone);
        advance(); assert(connects == 0 && !bt_reconnect_enabled());
    } else if (!strcmp(name, "power_off")) {
        linked(); state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone);
        acl(ESP_BT_STATUS_HCI_PEER_POWER_OFF, phone); advance(); assert(connects == 1);
    } else if (!strcmp(name, "foreign_event")) {
        linked(); state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone);
        acl(ESP_BT_STATUS_HCI_PEER_USER, other);
        acl(ESP_BT_STATUS_HCI_CONNECTION_TOUT, phone); advance(); assert(connects == 1);
    } else if (!strcmp(name, "unbonded")) {
        linked(); state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone);
        acl(ESP_BT_STATUS_HCI_CONNECTION_TOUT, phone); bond_count = 0;
        advance(); assert(connects == 0);
    } else if (!strcmp(name, "auth_failure")) {
        start(1); advance(); bt_reconnect_auth_event(phone, ESP_BT_STATUS_HCI_KEY_MISSING); drain();
        state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone); advance(); assert(connects == 1);
        assert(!bt_reconnect_enabled());
    } else if (!strcmp(name, "failed_attempt_local_close")) {
        start(1); advance(); state(ESP_HF_CLIENT_CONNECTION_STATE_CONNECTING, phone);
        acl(ESP_BT_STATUS_HCI_CONN_CAUSE_LOCAL_HOST, phone);
        state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone); advance(); assert(connects == 2);
    } else if (!strcmp(name, "new_phone")) {
        start(2); state(ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED, other);
        assert(!memcmp(saved, other, 6)); advance(); assert(connects == 0);
        state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, other);
        acl(ESP_BT_STATUS_HCI_CONNECTION_TOUT, other); advance();
        assert(connects == 1 && !memcmp(dialed, other, 6));
    } else if (!strcmp(name, "pause_resume")) {
        linked(); bt_reconnect_set_enabled(false); drain(); assert(disconnects == 1);
        state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone);
        acl(ESP_BT_STATUS_HCI_CONN_CAUSE_LOCAL_HOST, phone); advance(); assert(connects == 0);
        bt_reconnect_set_enabled(true); drain(); advance(); assert(connects == 1);
    } else if (!strcmp(name, "pause_pending")) {
        start(1); advance(); bt_reconnect_set_enabled(false); drain();
        assert(disconnects == 1 && !memcmp(disconnected_peer, phone, 6));
        state(ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED, phone);
        assert(!bt_reconnect_enabled() && disconnects == 2);
        state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone);
        acl(ESP_BT_STATUS_HCI_CONN_CAUSE_LOCAL_HOST, phone);
        advance(); assert(connects == 1);
    } else if (!strcmp(name, "resume_before_hfp_close") || !strcmp(name, "resume_before_acl_close")) {
        linked(); bt_reconnect_set_enabled(false); drain();
        bt_reconnect_set_enabled(true); drain();
        if (!strcmp(name, "resume_before_hfp_close")) {
            acl(ESP_BT_STATUS_HCI_CONN_CAUSE_LOCAL_HOST, phone);
            state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone);
        } else {
            state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone);
            acl(ESP_BT_STATUS_HCI_CONN_CAUSE_LOCAL_HOST, phone);
        }
        advance(); assert(bt_reconnect_enabled() && connects == 1);
    } else if (!strcmp(name, "foreign_hfp_disconnect")) {
        linked(); state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, other);
        advance(); assert(connects == 0);
        bt_reconnect_set_enabled(false); drain(); assert(disconnects == 1);
        bt_reconnect_set_enabled(true); drain();
        state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone);
        acl(ESP_BT_STATUS_HCI_CONNECTION_TOUT, phone); advance(); assert(connects == 1);
    } else if (!strcmp(name, "timer_failure")) {
        timer_result = ESP_FAIL; start(1); assert(strstr(last_event, "timer_error"));
    } else if (!strcmp(name, "nvs_failure")) {
        nvs_result = ESP_FAIL; linked(); assert(writes == 0);
        state(ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED, phone);
        acl(ESP_BT_STATUS_HCI_CONNECTION_TOUT, phone); advance(); assert(connects == 1);
    } else if (!strcmp(name, "bond_list_failure")) {
        list_result = ESP_FAIL; start(1); advance(); assert(connects == 0);
    } else {
        assert(!"unknown scenario");
    }
    puts("ok"); return 0;
}
"""


@pytest.fixture(scope="module")
def reconnect_harness(tmp_path_factory):
    source = ROOT / "firmware/main/bt_reconnect.c"
    assert source.exists(), "固件尚未实现主动蓝牙重连"
    compiler = shutil.which("cc")
    assert compiler, "固件重连测试需要本机 C 编译器"
    folder = tmp_path_factory.mktemp("firmware-reconnect")
    (folder / "sdk_stubs.h").write_text(STUBS)
    for header in ("esp_err.h", "esp_bt_defs.h", "esp_gap_bt_api.h", "esp_hf_client_api.h",
                   "esp_timer.h", "nvs.h", "uart_audio.h"):
        (folder / header).write_text('#include "sdk_stubs.h"\n')
    (folder / "harness.c").write_text(HARNESS)
    executable = folder / "reconnect"
    result = subprocess.run(
        [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(folder),
         "-I", str(ROOT / "firmware/main"), str(source), str(folder / "harness.c"),
         "-o", str(executable)], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


@pytest.mark.parametrize("scenario", [
    "no_bonds", "single_bond_start", "multiple_bonds", "saved_peer", "stale_saved_peer",
    "save_on_success", "range_hfp_first", "range_acl_first", "one_request", "retry_failure",
    "retry_without_connecting_callback", "immediate_failure", "manual_remote",
    "manual_local", "manual_acl_first", "power_off",
    "foreign_event", "unbonded", "auth_failure", "failed_attempt_local_close", "new_phone",
    "pause_resume", "pause_pending", "foreign_hfp_disconnect", "timer_failure",
    "nvs_failure", "bond_list_failure", "resume_before_hfp_close", "resume_before_acl_close",
])
def test_firmware_reconnect(reconnect_harness, scenario):
    result = subprocess.run([str(reconnect_harness), scenario], capture_output=True, text=True)
    assert result.returncode == 0, f"{scenario}: {result.stdout}{result.stderr}"
