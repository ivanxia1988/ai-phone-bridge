#include "audio_mixer.h"

#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/ringbuf.h"
#include "freertos/task.h"
#include "i2s_output.h"

#define MIX_TAG "AUD_MIX"

#define MIX_RINGBUF_BYTES      8192
#define MIX_FRAME_MS           10
#define MIX_MAX_FRAME_BYTES    640

static RingbufHandle_t s_uart_rb;
static RingbufHandle_t s_bt_rb;
static TaskHandle_t s_mix_task;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

static size_t s_uart_avail;
static size_t s_bt_avail;
static size_t s_frame_bytes;
static volatile bool s_enabled;
static volatile bool s_inited;

static bool ringbuf_read_exact(RingbufHandle_t rb, uint8_t *out, size_t len)
{
    size_t got = 0;
    while (got < len) {
        size_t item_size = 0;
        uint8_t *data = (uint8_t *)xRingbufferReceiveUpTo(rb, &item_size, 0, len - got);
        if (!data || item_size == 0) {
            return false;
        }
        memcpy(out + got, data, item_size);
        got += item_size;
        vRingbufferReturnItem(rb, data);
    }
    return true;
}

static void ringbuf_flush_all(RingbufHandle_t rb)
{
    while (true) {
        size_t item_size = 0;
        void *data = xRingbufferReceiveUpTo(rb, &item_size, 0, 1024);
        if (!data) {
            break;
        }
        vRingbufferReturnItem(rb, data);
    }
}

static inline int16_t sat_i16(int32_t v)
{
    if (v > 32767) {
        return 32767;
    }
    if (v < -32768) {
        return -32768;
    }
    return (int16_t)v;
}

static void audio_mixer_task(void *arg)
{
    (void)arg;
    uint8_t uart_buf[MIX_MAX_FRAME_BYTES];
    uint8_t bt_buf[MIX_MAX_FRAME_BYTES];
    uint8_t out_buf[MIX_MAX_FRAME_BYTES];
    const TickType_t tick_10ms = pdMS_TO_TICKS(MIX_FRAME_MS);
    TickType_t last_wake = xTaskGetTickCount();

    while (true) {
        if (!s_enabled || s_frame_bytes == 0) {
            vTaskDelay(tick_10ms);
            continue;
        }

        bool have_uart = false;
        bool have_bt = false;
        portENTER_CRITICAL(&s_mux);
        if (s_uart_avail >= s_frame_bytes) {
            s_uart_avail -= s_frame_bytes;
            have_uart = true;
        }
        if (s_bt_avail >= s_frame_bytes) {
            s_bt_avail -= s_frame_bytes;
            have_bt = true;
        }
        portEXIT_CRITICAL(&s_mux);

        if (have_uart) {
            if (!ringbuf_read_exact(s_uart_rb, uart_buf, s_frame_bytes)) {
                have_uart = false;
                memset(uart_buf, 0, s_frame_bytes);
                ringbuf_flush_all(s_uart_rb);
                portENTER_CRITICAL(&s_mux);
                s_uart_avail = 0;
                portEXIT_CRITICAL(&s_mux);
            }
        } else {
            memset(uart_buf, 0, s_frame_bytes);
        }

        if (have_bt) {
            if (!ringbuf_read_exact(s_bt_rb, bt_buf, s_frame_bytes)) {
                have_bt = false;
                memset(bt_buf, 0, s_frame_bytes);
                ringbuf_flush_all(s_bt_rb);
                portENTER_CRITICAL(&s_mux);
                s_bt_avail = 0;
                portEXIT_CRITICAL(&s_mux);
            }
        } else {
            memset(bt_buf, 0, s_frame_bytes);
        }

        const int16_t *u = (const int16_t *)uart_buf;
        const int16_t *b = (const int16_t *)bt_buf;
        int16_t *o = (int16_t *)out_buf;
        size_t samples = s_frame_bytes / 2;

        for (size_t i = 0; i < samples; i++) {
            int32_t mixed = ((int32_t)u[i] + (int32_t)b[i]) / 2;
            o[i] = sat_i16(mixed);
        }

        i2s_output_write(out_buf, s_frame_bytes);
        vTaskDelayUntil(&last_wake, tick_10ms);
    }
}

void audio_mixer_init(int sample_rate)
{
    if (s_inited) {
        return;
    }

    size_t frame_bytes = ((size_t)sample_rate * MIX_FRAME_MS / 1000) * 2;
    frame_bytes &= ~((size_t)1);
    if (frame_bytes == 0 || frame_bytes > MIX_MAX_FRAME_BYTES) {
        frame_bytes = 320;
    }
    s_frame_bytes = frame_bytes;

    s_uart_rb = xRingbufferCreate(MIX_RINGBUF_BYTES, RINGBUF_TYPE_BYTEBUF);
    s_bt_rb = xRingbufferCreate(MIX_RINGBUF_BYTES, RINGBUF_TYPE_BYTEBUF);
    if (!s_uart_rb || !s_bt_rb) {
        ESP_LOGE(MIX_TAG, "ring buffer alloc failed");
        return;
    }

    s_enabled = false;
    s_inited = true;
    xTaskCreate(audio_mixer_task, "audMix", 4096, NULL, configMAX_PRIORITIES - 5, &s_mix_task);
    ESP_LOGI(MIX_TAG, "audio mixer ready: frame=%u bytes", (unsigned)s_frame_bytes);
}

void audio_mixer_set_enabled(bool enabled)
{
    s_enabled = enabled;
    if (!enabled && s_inited) {
        ringbuf_flush_all(s_uart_rb);
        ringbuf_flush_all(s_bt_rb);
        portENTER_CRITICAL(&s_mux);
        s_uart_avail = 0;
        s_bt_avail = 0;
        portEXIT_CRITICAL(&s_mux);
    }
}

bool audio_mixer_is_enabled(void)
{
    return s_enabled;
}

static void audio_mixer_push_common(RingbufHandle_t rb, size_t *avail, const uint8_t *data, size_t len)
{
    if (!s_inited || !rb || !data || len < 2) {
        return;
    }

    size_t bytes = len & ~((size_t)1);
    if (bytes == 0) {
        return;
    }

    if (xRingbufferSend(rb, data, bytes, 0) == pdTRUE) {
        portENTER_CRITICAL(&s_mux);
        *avail += bytes;
        portEXIT_CRITICAL(&s_mux);
    }
}

void audio_mixer_push_uart(const uint8_t *data, size_t len)
{
    audio_mixer_push_common(s_uart_rb, &s_uart_avail, data, len);
}

void audio_mixer_push_bt(const uint8_t *data, size_t len)
{
    audio_mixer_push_common(s_bt_rb, &s_bt_avail, data, len);
}
