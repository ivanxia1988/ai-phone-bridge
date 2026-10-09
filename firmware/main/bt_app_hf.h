/*
 * SPDX-FileCopyrightText: 2021 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Unlicense OR CC0-1.0
 */

#ifndef __BT_APP_HF_H__
#define __BT_APP_HF_H__

#include <stdint.h>
#include "esp_hf_client_api.h"


#define BT_HF_TAG               "BT_HF"

/**
 * @brief     callback function for HF client
 */
void bt_app_hf_client_cb(esp_hf_client_cb_event_t event, esp_hf_client_cb_param_t *param);

// Handle control command payload from UART bridge (dir=2 packet payload).
// Supported commands include "hangup", "dtmf:<c>", "audio_on", "audio_off",
// "bt_reconnect" (resume retries) and "bt_disconnect" (pause and disconnect).
bool bt_app_hf_handle_uart_control(const uint8_t *payload, uint16_t len);


#endif /* __BT_APP_HF_H__*/
