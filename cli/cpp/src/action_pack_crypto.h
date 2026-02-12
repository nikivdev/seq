#pragma once

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace action_pack_crypto {

// Key management: uses the macOS Keychain for private keys.
//
// key_id is a caller-chosen identifier (e.g. "default"). The public key is exported as
// base64 of the Security "external representation" for an EC P-256 public key.
bool keygen_p256(std::string_view key_id, std::string* pubkey_b64, std::string* error);
bool export_pubkey_p256(std::string_view key_id, std::string* pubkey_b64, std::string* error);

// Sign/verify the raw payload bytes.
bool sign_p256(std::string_view key_id,
               std::string_view payload,
               std::vector<uint8_t>* signature,
               std::string* error);

bool verify_p256(std::string_view pubkey_external_b64,
                 std::string_view payload,
                 std::string_view signature,
                 std::string* error);

}  // namespace action_pack_crypto

