/*
 * SPDX-FileCopyrightText: 2026
 *
 * SPDX-License-Identifier: Unlicense OR CC0-1.0
 */

#include "uart_audio.h"

#include <inttypes.h>
#include <stdbool.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "driver/uart.h"
#include "esp_hf_client_api.h"
#include "bt_app_hf.h"
#include "audio_mixer.h"
#include "i2s_output.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "freertos/semphr.h"

#define UART_AUDIO_TAG "UART_AUD"

#define UART_AUDIO_UART_NUM UART_NUM_0
#define UART_AUDIO_BAUDRATE 921600

// Default pins (adjust here if your wiring differs)
#define UART_AUDIO_TX_GPIO UART_PIN_NO_CHANGE
#define UART_AUDIO_RX_GPIO UART_PIN_NO_CHANGE

#define UART_AUDIO_RX_BUF_SIZE 8192
#define UART_AUDIO_TX_BUF_SIZE 8192

#define UART_AUDIO_TX_RINGBUF_SIZE 16384
#define UART_AUDIO_UL_RINGBUF_SIZE 8192

#define UART_AUDIO_MAX_PAYLOAD 512

// Uplink backlog policy: keep latency bounded by dropping old data when the
// consumer (BT stack) can't keep up. This emulates a lossy link rather than
// accumulating delay.
#define UART_AUDIO_UL_MAX_BACKLOG_BYTES 1024

// Packet (before COBS):
//   dir:1, seq:2, len:2, payload:len
#define UART_AUDIO_HDR_SIZE 5

/* I2S playback route:
 * 0 = mix UART uplink (dir=1) and BT downlink
 * 1 = UART uplink (dir=1) direct to I2S ring buffer
 * 2 = BT downlink direct to I2S ring buffer
 */
#define UART_AUDIO_I2S_ROUTE_MIXED      0
#define UART_AUDIO_I2S_ROUTE_UART_ONLY  1
#define UART_AUDIO_I2S_ROUTE_BT_ONLY    2
#define UART_AUDIO_I2S_ROUTE            UART_AUDIO_I2S_ROUTE_BT_ONLY

static RingbufHandle_t s_tx_rb;
static RingbufHandle_t s_ul_rb;
static SemaphoreHandle_t s_ul_lock;
static bool s_flush_requested;
static uint16_t s_dl_seq;

static portMUX_TYPE s_ul_mux = portMUX_INITIALIZER_UNLOCKED;
static size_t s_ul_available;

static uint32_t s_ul_rx_bytes;
static uint32_t s_ul_tx_bytes;
static uint32_t s_ul_rb_dropped;
static uint32_t s_tx_rb_dropped;

static uint16_t s_ul_last_peak;
static uint32_t s_ul_last_mean_abs;

static esp_timer_handle_t s_stats_timer;

static volatile bool s_audio_connected;

static void uart_audio_ul_flush_internal(void);
static void uart_audio_ul_flush_locked(void);
static size_t cobs_encode(const uint8_t *input, size_t length, uint8_t *output);

#if I2S_OUTPUT_USE_TEST_TONE
static uint32_t s_i2s_test_phase_q32;
static TaskHandle_t s_i2s_tone_task;

static void uart_audio_i2s_write_test_tone(size_t requested_bytes)
{
    size_t bytes = requested_bytes & ~((size_t)1); // int16 alignment
    if (bytes == 0) {
        return;
    }
    if (bytes > UART_AUDIO_MAX_PAYLOAD) {
        bytes = UART_AUDIO_MAX_PAYLOAD;
    }

    int16_t pcm[UART_AUDIO_MAX_PAYLOAD / 2];
    size_t samples = bytes / 2;
    uint32_t step_q32 = (uint32_t)(((uint64_t)I2S_OUTPUT_TEST_TONE_HZ << 32) / I2S_OUTPUT_DEFAULT_RATE);

    for (size_t i = 0; i < samples; i++) {
        s_i2s_test_phase_q32 += step_q32;
        pcm[i] = (s_i2s_test_phase_q32 & 0x80000000U) ? I2S_OUTPUT_TEST_TONE_AMP : -I2S_OUTPUT_TEST_TONE_AMP;
    }

    i2s_output_write_direct((const uint8_t *)pcm, samples * 2);
}

static void uart_audio_i2s_tone_task(void *arg)
{
    (void)arg;
    const TickType_t delay_ticks = pdMS_TO_TICKS(8); // ~7.5ms per 240 bytes @16k
    while (true) {
        uart_audio_i2s_write_test_tone(240);
        vTaskDelay(delay_ticks);
    }
}
#endif

static void uart_audio_send_packet(uint8_t dir, const uint8_t *data, size_t len)
{
    if (!s_tx_rb || !data || len == 0) {
        return;
    }

    while (len > 0) {
        size_t chunk = len;
        if (chunk > UART_AUDIO_MAX_PAYLOAD) {
            chunk = UART_AUDIO_MAX_PAYLOAD;
        }

        uint8_t pkt[UART_AUDIO_HDR_SIZE + chunk];
        pkt[0] = dir;
        uint16_t seq = s_dl_seq++;
        pkt[1] = (uint8_t)(seq & 0xFF);
        pkt[2] = (uint8_t)((seq >> 8) & 0xFF);
        pkt[3] = (uint8_t)(chunk & 0xFF);
        pkt[4] = (uint8_t)((chunk >> 8) & 0xFF);
        memcpy(&pkt[UART_AUDIO_HDR_SIZE], data, chunk);

        uint8_t encoded[(UART_AUDIO_HDR_SIZE + chunk) + ((UART_AUDIO_HDR_SIZE + chunk) / 254) + 2];
        size_t encoded_len = cobs_encode(pkt, UART_AUDIO_HDR_SIZE + chunk, encoded);
        encoded[encoded_len++] = 0;

        BaseType_t ok = xRingbufferSend(s_tx_rb, encoded, encoded_len, 0);
        if (ok != pdTRUE) {
            s_tx_rb_dropped += (uint32_t)encoded_len;
            break;
        }

        data += chunk;
        len -= chunk;
    }
}

void uart_audio_set_audio_connected(bool connected)
{
    s_audio_connected = connected;
    if (!connected) {
        uart_audio_ul_flush_internal();
    }
}

static void uart_audio_ul_flush_internal(void)
{
    if (!s_ul_rb || !s_ul_lock) return;
    __atomic_store_n(&s_flush_requested, true, __ATOMIC_RELEASE);
    if (xSemaphoreTake(s_ul_lock, 0) == pdTRUE) {
        uart_audio_ul_flush_locked();
        xSemaphoreGive(s_ul_lock);
    }
}

/* Caller owns s_ul_lock: ring contents and byte count change together. */
static void uart_audio_ul_flush_locked(void)
{
    __atomic_store_n(&s_flush_requested, false, __ATOMIC_RELEASE);
    while (true) {
        size_t item_size = 0;
        void *data = xRingbufferReceiveUpTo(s_ul_rb, &item_size, 0, 1024);
        if (!data) {
            break;
        }
        vRingbufferReturnItem(s_ul_rb, data);
    }

    portENTER_CRITICAL(&s_ul_mux);
    s_ul_available = 0;
    portEXIT_CRITICAL(&s_ul_mux);
}

static void uart_audio_stats_timer_cb(void *arg)
{
    (void)arg;
    size_t avail;
    portENTER_CRITICAL(&s_ul_mux);
    avail = s_ul_available;
    portEXIT_CRITICAL(&s_ul_mux);

    uint16_t peak = s_ul_last_peak;
    uint32_t mean_abs = s_ul_last_mean_abs;

    uint32_t i2s_rb_in = 0, i2s_rb_drop = 0, i2s_dma_out = 0;
    i2s_output_get_stats(&i2s_rb_in, &i2s_rb_drop, &i2s_dma_out);

    ESP_LOGI(
        UART_AUDIO_TAG,
        "UL stats: rx=%" PRIu32 " tx=%" PRIu32 " avail=%u rb_drop=%" PRIu32 " tx_drop=%" PRIu32
        " pcm_peak=%u mean=%" PRIu32
        " | I2S rb_in=%" PRIu32 " rb_drop=%" PRIu32 " dma_out=%" PRIu32,
        s_ul_rx_bytes,
        s_ul_tx_bytes,
        (unsigned)avail,
        s_ul_rb_dropped,
        s_tx_rb_dropped,
        (unsigned)peak,
        mean_abs,
        i2s_rb_in,
        i2s_rb_drop,
        i2s_dma_out);
}

static TaskHandle_t s_rx_task;
static TaskHandle_t s_tx_task;

static size_t cobs_encode(const uint8_t *input, size_t length, uint8_t *output)
{
    size_t read_index = 0;
    size_t write_index = 1;
    size_t code_index = 0;
    uint8_t code = 1;

    while (read_index < length) {
        if (input[read_index] == 0) {
            output[code_index] = code;
            code = 1;
            code_index = write_index++;
            read_index++;
        } else {
            output[write_index++] = input[read_index++];
            code++;
            if (code == 0xFF) {
                output[code_index] = code;
                code = 1;
                code_index = write_index++;
            }
        }
    }

    output[code_index] = code;
    return write_index;
}

static bool cobs_decode(const uint8_t *input, size_t length, uint8_t *output, size_t *out_len)
{
    if (length == 0) {
        return false;
    }

    size_t read_index = 0;
    size_t write_index = 0;

    while (read_index < length) {
        uint8_t code = input[read_index];
        if (code == 0) {
            return false;
        }
        read_index++;

        size_t copy_len = (size_t)code - 1;
        if (read_index + copy_len > length) {
            return false;
        }

        memcpy(&output[write_index], &input[read_index], copy_len);
        read_index += copy_len;
        write_index += copy_len;

        if (code != 0xFF && read_index < length) {
            output[write_index++] = 0;
        }
    }

    *out_len = write_index;
    return true;
}

static void uart_audio_tx_task(void *arg)
{
    (void)arg;
    while (true) {
        size_t item_size = 0;
        uint8_t *data = (uint8_t *)xRingbufferReceiveUpTo(s_tx_rb, &item_size, portMAX_DELAY, 512);
        if (!data || item_size == 0) {
            continue;
        }
        int written = uart_write_bytes(UART_AUDIO_UART_NUM, data, (uint32_t)item_size);
        (void)written;
        vRingbufferReturnItem(s_tx_rb, data);
    }
}

static void uart_audio_handle_packet(const uint8_t *pkt, size_t pkt_len)
{
    if (pkt_len < UART_AUDIO_HDR_SIZE) {
        return;
    }

    uint8_t dir = pkt[0];
    uint16_t len = (uint16_t)pkt[3] | ((uint16_t)pkt[4] << 8);
    if (UART_AUDIO_HDR_SIZE + len != pkt_len) {
        return;
    }

    if (len == 0) {
        return;
    }

    if (dir == 3) {
        uart_audio_send_packet(3, &pkt[UART_AUDIO_HDR_SIZE], len);
        return;
    }

    if (dir == 2) {
        (void)bt_app_hf_handle_uart_control(&pkt[UART_AUDIO_HDR_SIZE], len);
        return;
    }

    if (dir != 1 || (len & 1) || !s_audio_connected) {
        return;
    }

    // Bluetooth callbacks never block waiting for this mutex.
    if (xSemaphoreTake(s_ul_lock, 0) != pdTRUE) {
        s_ul_rb_dropped += len;
        return;
    }
    if (__atomic_load_n(&s_flush_requested, __ATOMIC_ACQUIRE)) {
        uart_audio_ul_flush_locked();
    }
    // If backlog is growing, drop old buffered audio to avoid building latency.
    size_t avail;
    portENTER_CRITICAL(&s_ul_mux);
    avail = s_ul_available;
    portEXIT_CRITICAL(&s_ul_mux);
    if (avail >= UART_AUDIO_UL_MAX_BACKLOG_BYTES) {
        uart_audio_ul_flush_locked();
    }

    /* Forward to I2S according to playback route (or ignore in test-tone mode). */
#if I2S_OUTPUT_USE_TEST_TONE
    (void)len;
#else
  #if UART_AUDIO_I2S_ROUTE == UART_AUDIO_I2S_ROUTE_MIXED
    audio_mixer_push_uart(&pkt[UART_AUDIO_HDR_SIZE], len);
  #elif UART_AUDIO_I2S_ROUTE == UART_AUDIO_I2S_ROUTE_UART_ONLY
    i2s_output_write(&pkt[UART_AUDIO_HDR_SIZE], len);
  #elif UART_AUDIO_I2S_ROUTE == UART_AUDIO_I2S_ROUTE_BT_ONLY
    /* UART source is not used for I2S in BT-only route. */
  #else
  #error "Invalid UART_AUDIO_I2S_ROUTE"
  #endif
#endif

    /* Track PCM level at ingress so stats work even without BT connection */
    /* The five-byte header makes PCM potentially unaligned on Xtensa. */
    const uint8_t *pcm_bytes = &pkt[UART_AUDIO_HDR_SIZE];
    size_t samples = len / 2;
    if (samples > 0) {
        size_t n = samples > 120 ? 120 : samples;
        uint32_t sum_abs = 0;
        uint16_t peak = 0;
        for (size_t i = 0; i < n; i++) {
            int32_t v = (int16_t)((uint16_t)pcm_bytes[i * 2] |
                                 ((uint16_t)pcm_bytes[i * 2 + 1] << 8));
            if (v < 0) v = -v;
            sum_abs += (uint32_t)v;
            if ((uint32_t)v > peak) peak = (uint16_t)v;
        }
        s_ul_last_peak = peak;
        s_ul_last_mean_abs = sum_abs / (uint32_t)n;
    }

    BaseType_t ok = xRingbufferSend(s_ul_rb, (void *)&pkt[UART_AUDIO_HDR_SIZE], len, 0);
    if (ok != pdTRUE) {
        s_ul_rb_dropped += len;
        xSemaphoreGive(s_ul_lock);
        return;
    }

    s_ul_rx_bytes += len;

    portENTER_CRITICAL(&s_ul_mux);
    s_ul_available += len;
    portEXIT_CRITICAL(&s_ul_mux);
    xSemaphoreGive(s_ul_lock);

    if (s_audio_connected) {
        esp_hf_client_outgoing_data_ready();
    }
}

static void uart_audio_rx_task(void *arg)
{
    (void)arg;

    uint8_t rx_byte;
    uint8_t cobs_frame[UART_AUDIO_HDR_SIZE + UART_AUDIO_MAX_PAYLOAD + 32];
    uint8_t decoded[UART_AUDIO_HDR_SIZE + UART_AUDIO_MAX_PAYLOAD + 32];

    size_t frame_len = 0;
    bool dropping = false;

    while (true) {
        int r = uart_read_bytes(UART_AUDIO_UART_NUM, &rx_byte, 1, portMAX_DELAY);
        if (r <= 0) {
            continue;
        }

        if (rx_byte == 0) {
            if (frame_len > 0 && !dropping) {
                size_t out_len = 0;
                if (cobs_decode(cobs_frame, frame_len, decoded, &out_len)) {
                    uart_audio_handle_packet(decoded, out_len);
                }
                frame_len = 0;
            }
            frame_len = 0;
            dropping = false;
            continue;
        }

        if (dropping) continue;
        if (frame_len >= sizeof(cobs_frame)) {
            dropping = true;
            frame_len = 0;
            continue;
        }

        cobs_frame[frame_len++] = rx_byte;
    }
}

void uart_audio_init(void)
{
    static bool inited;
    if (inited) {
        return;
    }
    inited = true;

    uart_config_t cfg = {
        .baud_rate = UART_AUDIO_BAUDRATE,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };

    ESP_ERROR_CHECK(uart_driver_install(UART_AUDIO_UART_NUM, UART_AUDIO_RX_BUF_SIZE, UART_AUDIO_TX_BUF_SIZE, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(UART_AUDIO_UART_NUM, &cfg));
    ESP_ERROR_CHECK(uart_set_pin(UART_AUDIO_UART_NUM, UART_AUDIO_TX_GPIO, UART_AUDIO_RX_GPIO, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));

    s_tx_rb = xRingbufferCreate(UART_AUDIO_TX_RINGBUF_SIZE, RINGBUF_TYPE_BYTEBUF);
    s_ul_rb = xRingbufferCreate(UART_AUDIO_UL_RINGBUF_SIZE, RINGBUF_TYPE_BYTEBUF);
    s_ul_lock = xSemaphoreCreateMutex();

    if (!s_tx_rb || !s_ul_rb || !s_ul_lock) {
        ESP_LOGE(UART_AUDIO_TAG, "ringbuffer alloc failed");
        return;
    }

    xTaskCreate(uart_audio_rx_task, "uartAudRx", 4096, NULL, configMAX_PRIORITIES - 4, &s_rx_task);
    xTaskCreate(uart_audio_tx_task, "uartAudTx", 4096, NULL, configMAX_PRIORITIES - 4, &s_tx_task);

    if (!s_stats_timer) {
        const esp_timer_create_args_t targs = {
            .callback = &uart_audio_stats_timer_cb,
            .arg = NULL,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "uartAudStats",
            .skip_unhandled_events = true,
        };
        if (esp_timer_create(&targs, &s_stats_timer) == ESP_OK) {
            (void)esp_timer_start_periodic(s_stats_timer, 2000000); // 2s
        }
    }

    ESP_LOGI(UART_AUDIO_TAG, "UART2 audio ready: TX=%d RX=%d baud=%d", UART_AUDIO_TX_GPIO, UART_AUDIO_RX_GPIO, UART_AUDIO_BAUDRATE);
#if I2S_OUTPUT_USE_TEST_TONE
    ESP_LOGI(UART_AUDIO_TAG, "I2S test tone enabled: %d Hz amp=%d", I2S_OUTPUT_TEST_TONE_HZ, I2S_OUTPUT_TEST_TONE_AMP);
    xTaskCreate(uart_audio_i2s_tone_task, "i2sTone", 3072, NULL, configMAX_PRIORITIES - 5, &s_i2s_tone_task);
#else
  #if UART_AUDIO_I2S_ROUTE == UART_AUDIO_I2S_ROUTE_MIXED
    audio_mixer_init(I2S_OUTPUT_DEFAULT_RATE);
    audio_mixer_set_enabled(true);
    ESP_LOGI(UART_AUDIO_TAG, "I2S route: mixed (UART + BT)");
  #elif UART_AUDIO_I2S_ROUTE == UART_AUDIO_I2S_ROUTE_UART_ONLY
    ESP_LOGI(UART_AUDIO_TAG, "I2S route: UART only (direct)");
  #elif UART_AUDIO_I2S_ROUTE == UART_AUDIO_I2S_ROUTE_BT_ONLY
    ESP_LOGI(UART_AUDIO_TAG, "I2S route: BT only (direct)");
  #else
  #error "Invalid UART_AUDIO_I2S_ROUTE"
  #endif
#endif
}

void uart_audio_send_downlink(const uint8_t *data, size_t len)
{
#if !I2S_OUTPUT_USE_TEST_TONE
  #if UART_AUDIO_I2S_ROUTE == UART_AUDIO_I2S_ROUTE_MIXED
    audio_mixer_push_bt(data, len);
  #elif UART_AUDIO_I2S_ROUTE == UART_AUDIO_I2S_ROUTE_UART_ONLY
    /* BT source is not used for I2S in UART-only route. */
  #elif UART_AUDIO_I2S_ROUTE == UART_AUDIO_I2S_ROUTE_BT_ONLY
    i2s_output_write(data, len);
  #else
  #error "Invalid UART_AUDIO_I2S_ROUTE"
  #endif
#endif
    uart_audio_send_packet(0, data, len);
}

void uart_audio_send_control_event(const char *text)
{
    if (!text || text[0] == '\0') {
        return;
    }
    uart_audio_send_packet(2, (const uint8_t *)text, strlen(text));
}

void uart_audio_get_uplink(uint8_t *out, size_t len)
{
    if (out && !uart_audio_try_get_uplink(out, len)) memset(out, 0, len);
}

bool uart_audio_try_get_uplink(uint8_t *out, size_t len)
{
    if (!out || len == 0) {
        return false;
    }
    if (!s_ul_rb || !s_ul_lock || xSemaphoreTake(s_ul_lock, 0) != pdTRUE) {
        return false;
    }
    if (__atomic_load_n(&s_flush_requested, __ATOMIC_ACQUIRE)) {
        uart_audio_ul_flush_locked();
    }

    portENTER_CRITICAL(&s_ul_mux);
    bool enough = (s_ul_available >= len);
    portEXIT_CRITICAL(&s_ul_mux);
    if (!enough) {
        xSemaphoreGive(s_ul_lock);
        return false;
    }

    size_t got = 0;
    while (got < len) {
        size_t item_size = 0;
        uint8_t *data = (uint8_t *)xRingbufferReceiveUpTo(s_ul_rb, &item_size, 0, len - got);
        if (!data || item_size == 0) {
            // Should not happen if s_ul_available was accurate; fail safe.
            break;
        }
        memcpy(out + got, data, item_size);
        got += item_size;
        vRingbufferReturnItem(s_ul_rb, data);
    }

    if (got != len) {
        // We consumed some bytes but couldn't satisfy a full frame; drop them to avoid
        // desynchronizing the stream and report underflow.
        portENTER_CRITICAL(&s_ul_mux);
        if (s_ul_available >= got) {
            s_ul_available -= got;
        } else {
            s_ul_available = 0;
        }
        portEXIT_CRITICAL(&s_ul_mux);
        xSemaphoreGive(s_ul_lock);
        return false;
    }

    portENTER_CRITICAL(&s_ul_mux);
    s_ul_available -= len;
    portEXIT_CRITICAL(&s_ul_mux);
    s_ul_tx_bytes += (uint32_t)len;
    xSemaphoreGive(s_ul_lock);
    return true;
}

void uart_audio_flush_uplink(void)
{
    if (!s_ul_rb) {
        return;
    }

    uart_audio_ul_flush_internal();
}
