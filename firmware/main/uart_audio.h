/*
 * SPDX-FileCopyrightText: 2026
 *
 * SPDX-License-Identifier: Unlicense OR CC0-1.0
 */

#pragma once

#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

void uart_audio_init(void);

// Notify UART audio bridge whether HFP SCO audio is connected.
// Used to gate esp_hf_client_outgoing_data_ready() to valid states.
void uart_audio_set_audio_connected(bool connected);

// Downlink: AG -> ESP32 -> UART2 -> PC
void uart_audio_send_downlink(const uint8_t *data, size_t len);

// Uplink: PC -> UART2 -> ESP32 -> AG
// Always fills exactly len bytes (silence padded if insufficient).
void uart_audio_get_uplink(uint8_t *out, size_t len);

// Uplink: attempt to read exactly len bytes.
// Returns true only if enough data was available and copied to out.
// If false, out is left untouched.
bool uart_audio_try_get_uplink(uint8_t *out, size_t len);

void uart_audio_flush_uplink(void);

// Send control/status text event to host over UART bridge (dir=2 packet payload).
void uart_audio_send_control_event(const char *text);

#ifdef __cplusplus
}
#endif
