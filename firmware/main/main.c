/* SPDX-License-Identifier: Unlicense OR CC0-1.0
 * Adapted from Espressif's HFP client sample and 5iop/esp32-hfp-bridge.
 * UART0 belongs exclusively to the framed USB transport once the app starts.
 */
#include <string.h>
#include "nvs_flash.h"
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_bt_device.h"
#include "esp_gap_bt_api.h"
#include "esp_hf_client_api.h"
#include "esp_log.h"
#include "bt_app_core.h"
#include "bt_app_hf.h"
#include "bt_reconnect.h"
#include "uart_audio.h"

esp_bd_addr_t peer_addr = {0};

static void gap_callback(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *p)
{
    switch (event) {
    case ESP_BT_GAP_AUTH_CMPL_EVT:
        uart_audio_send_control_event(p->auth_cmpl.stat == ESP_BT_STATUS_SUCCESS ?
                                      "evt:paired" : "evt:pair_failed");
        bt_reconnect_auth_event(p->auth_cmpl.bda, p->auth_cmpl.stat);
        break;
    case ESP_BT_GAP_ACL_DISCONN_CMPL_STAT_EVT:
        bt_reconnect_acl_disconnected(p->acl_disconn_cmpl_stat.bda,
                                     p->acl_disconn_cmpl_stat.reason);
        break;
    case ESP_BT_GAP_PIN_REQ_EVT: {
        esp_bt_pin_code_t pin = {'0', '0', '0', '0'};
        esp_bt_gap_pin_reply(p->pin_req.bda, true, 4, pin);
        break;
    }
#if CONFIG_BT_SSP_ENABLED
    case ESP_BT_GAP_CFM_REQ_EVT:
        esp_bt_gap_ssp_confirm_reply(p->cfm_req.bda, true);
        break;
#endif
    default: break;
    }
}

static void stack_up(uint16_t event, void *param)
{
    (void)event; (void)param;
    ESP_ERROR_CHECK(esp_bt_dev_set_device_name("ESP32 Phone Bridge"));
    ESP_ERROR_CHECK(esp_bt_gap_register_callback(gap_callback));
    ESP_ERROR_CHECK(esp_hf_client_register_callback(bt_app_hf_client_cb));
    ESP_ERROR_CHECK(esp_hf_client_init());
    esp_bt_cod_t cod = {
        .major = ESP_BT_COD_MAJOR_DEV_AV,
        .minor = 2, /* Bluetooth Assigned Numbers: Audio/Video hands-free. */
        .service = ESP_BT_COD_SRVC_AUDIO | ESP_BT_COD_SRVC_TELEPHONY,
    };
    ESP_ERROR_CHECK(esp_bt_gap_set_cod(cod, ESP_BT_INIT_COD));
    esp_bt_io_cap_t capability = ESP_BT_IO_CAP_NONE;
    ESP_ERROR_CHECK(esp_bt_gap_set_security_param(ESP_BT_SP_IOCAP_MODE,
                                                &capability, sizeof(capability)));
    ESP_ERROR_CHECK(esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE));
    uart_audio_send_control_event("evt:ready firmware=1 bt_build=2 baud=921600");
}

void app_main(void)
{
    esp_log_level_set("*", ESP_LOG_NONE);
    ESP_ERROR_CHECK(nvs_flash_init());
    /* UART queries can arrive while the Bluetooth controller is starting. */
    bt_app_task_start_up();
    uart_audio_init();
    ESP_ERROR_CHECK(esp_bt_controller_mem_release(ESP_BT_MODE_BLE));
    esp_bt_controller_config_t config = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_bt_controller_init(&config));
    ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT));
    ESP_ERROR_CHECK(esp_bluedroid_init());
    ESP_ERROR_CHECK(esp_bluedroid_enable());
    bt_app_work_dispatch(stack_up, 0, NULL, 0, NULL);
}
