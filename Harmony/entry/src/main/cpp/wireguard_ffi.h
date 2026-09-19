#ifndef WIREGUARD_FFI_H
#define WIREGUARD_FFI_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    WIREGUARD_DONE = 0,
    WRITE_TO_NETWORK = 1,
    WIREGUARD_ERROR = 2,
    WRITE_TO_TUNNEL_IPV4 = 4,
    WRITE_TO_TUNNEL_IPV6 = 6,
} result_type;

typedef struct {
    result_type op;
    size_t size;
} wireguard_result;

typedef struct {
    int64_t time_since_last_handshake;
    size_t tx_bytes;
    size_t rx_bytes;
    float estimated_loss;
    int32_t estimated_rtt;
    uint8_t reserved[56];
} stats;

typedef struct {
    uint8_t key[32];
} x25519_key;

x25519_key x25519_secret_key(void);
x25519_key x25519_public_key(x25519_key private_key);
const char* x25519_key_to_base64(x25519_key key);
const char* x25519_key_to_hex(x25519_key key);
void x25519_key_to_str_free(char* stringified_key);
int32_t check_base64_encoded_x25519_key(const char* key);

typedef void MutexTunn;
MutexTunn* new_tunnel(const char* static_private,
                      const char* server_static_public,
                      const char* preshared_key,
                      uint16_t keep_alive,
                      uint32_t index);
void tunnel_free(MutexTunn* tunnel);

wireguard_result wireguard_write(const MutexTunn* tunnel,
                                 const uint8_t* src, uint32_t src_size,
                                 uint8_t* dst, uint32_t dst_size);
wireguard_result wireguard_read(const MutexTunn* tunnel,
                                const uint8_t* src, uint32_t src_size,
                                uint8_t* dst, uint32_t dst_size);
wireguard_result wireguard_tick(const MutexTunn* tunnel,
                                uint8_t* dst, uint32_t dst_size);
wireguard_result wireguard_force_handshake(const MutexTunn* tunnel,
                                           uint8_t* dst, uint32_t dst_size);
stats wireguard_stats(const MutexTunn* tunnel);

#ifdef __cplusplus
}
#endif

#endif
