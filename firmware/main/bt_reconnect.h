/* SPDX-License-Identifier: Unlicense OR CC0-1.0 */
#pragma once

#include <stdbool.h>
#include "esp_bt_defs.h"
#include "esp_hf_client_api.h"

/* Events are copied to the BT application queue; callers need not retain bda. */
void bt_reconnect_start(void);
void bt_reconnect_hfp_event(esp_hf_client_connection_state_t state, const uint8_t *bda);
void bt_reconnect_acl_disconnected(const uint8_t *bda, esp_bt_status_t reason);
void bt_reconnect_auth_event(const uint8_t *bda, esp_bt_status_t status);
void bt_reconnect_set_enabled(bool enabled);

/* Read only from the BT application task, e.g. UART status handling. */
bool bt_reconnect_enabled(void);
bool bt_reconnect_pending(void);
