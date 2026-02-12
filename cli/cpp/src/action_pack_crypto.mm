#include "action_pack_crypto.h"

#include "base64.h"

#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>

#include <cstring>
#include <memory>
#include <type_traits>

namespace action_pack_crypto {
namespace {

struct CFDeleter {
  void operator()(CFTypeRef v) const {
    if (v) CFRelease(v);
  }
};

template <typename T>
using CFRef = std::unique_ptr<std::remove_pointer_t<T>, CFDeleter>;

CFRef<CFDataRef> make_tag(std::string_view key_id) {
  // Avoid collisions with anything else in the user's keychain.
  std::string tag = "dev.nikiv.seq.action_pack." + std::string(key_id);
  CFRef<CFDataRef> data((CFDataRef)CFDataCreate(kCFAllocatorDefault,
                                                reinterpret_cast<const UInt8*>(tag.data()),
                                                (CFIndex)tag.size()));
  return data;
}

CFRef<SecKeyRef> find_private_key(std::string_view key_id) {
  auto tag = make_tag(key_id);
  if (!tag) return CFRef<SecKeyRef>(nullptr);

  CFRef<CFMutableDictionaryRef> query((CFMutableDictionaryRef)CFDictionaryCreateMutable(
      kCFAllocatorDefault, 0, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks));
  if (!query) return CFRef<SecKeyRef>(nullptr);

  CFDictionarySetValue(query.get(), kSecClass, kSecClassKey);
  CFDictionarySetValue(query.get(), kSecAttrKeyType, kSecAttrKeyTypeECSECPrimeRandom);
  CFDictionarySetValue(query.get(), kSecAttrKeyClass, kSecAttrKeyClassPrivate);
  CFDictionarySetValue(query.get(), kSecAttrApplicationTag, tag.get());
  CFDictionarySetValue(query.get(), kSecReturnRef, kCFBooleanTrue);

  SecKeyRef key = nullptr;
  OSStatus st = SecItemCopyMatching(query.get(), (CFTypeRef*)&key);
  if (st != errSecSuccess || !key) {
    return CFRef<SecKeyRef>(nullptr);
  }
  return CFRef<SecKeyRef>(key);
}

CFRef<SecKeyRef> create_public_key_from_external(const std::vector<uint8_t>& external) {
  CFRef<CFDataRef> data((CFDataRef)CFDataCreate(kCFAllocatorDefault,
                                                external.data(),
                                                (CFIndex)external.size()));
  if (!data) return CFRef<SecKeyRef>(nullptr);

  CFRef<CFMutableDictionaryRef> attrs((CFMutableDictionaryRef)CFDictionaryCreateMutable(
      kCFAllocatorDefault, 0, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks));
  if (!attrs) return CFRef<SecKeyRef>(nullptr);

  int bits = 256;
  CFRef<CFNumberRef> bits_num((CFNumberRef)CFNumberCreate(kCFAllocatorDefault, kCFNumberIntType, &bits));
  CFDictionarySetValue(attrs.get(), kSecAttrKeyType, kSecAttrKeyTypeECSECPrimeRandom);
  CFDictionarySetValue(attrs.get(), kSecAttrKeyClass, kSecAttrKeyClassPublic);
  if (bits_num) {
    CFDictionarySetValue(attrs.get(), kSecAttrKeySizeInBits, bits_num.get());
  }

  CFErrorRef cf_err = nullptr;
  SecKeyRef key = SecKeyCreateWithData(data.get(), attrs.get(), &cf_err);
  if (cf_err) {
    CFRelease(cf_err);
  }
  return CFRef<SecKeyRef>(key);
}

bool copy_external_pubkey_b64(SecKeyRef priv, std::string* out_b64, std::string* error) {
  if (!out_b64) return false;
  out_b64->clear();
  if (!priv) {
    if (error) *error = "missing private key";
    return false;
  }
  CFRef<SecKeyRef> pub(SecKeyCopyPublicKey(priv));
  if (!pub) {
    if (error) *error = "unable to copy public key";
    return false;
  }
  CFErrorRef cf_err = nullptr;
  CFDataRef ext = SecKeyCopyExternalRepresentation(pub.get(), &cf_err);
  if (cf_err) {
    CFRelease(cf_err);
  }
  CFRef<CFDataRef> ext_ref(ext);
  if (!ext_ref) {
    if (error) *error = "unable to export public key";
    return false;
  }
  const UInt8* bytes = CFDataGetBytePtr(ext_ref.get());
  CFIndex n = CFDataGetLength(ext_ref.get());
  if (!bytes || n <= 0) {
    if (error) *error = "empty public key bytes";
    return false;
  }
  *out_b64 = base64::encode(reinterpret_cast<const uint8_t*>(bytes), (size_t)n);
  return true;
}

std::string cf_error_to_string(CFErrorRef err) {
  if (!err) return {};
  CFStringRef desc = CFErrorCopyDescription(err);
  if (!desc) return {};
  CFDeleter del;
  del(desc);
  CFIndex len = CFStringGetLength(desc);
  if (len <= 0) return {};
  CFIndex max = CFStringGetMaximumSizeForEncoding(len, kCFStringEncodingUTF8) + 1;
  std::string out;
  out.resize((size_t)max);
  if (CFStringGetCString(desc, out.data(), max, kCFStringEncodingUTF8)) {
    out.resize(std::strlen(out.c_str()));
    return out;
  }
  return {};
}

}  // namespace

bool keygen_p256(std::string_view key_id, std::string* pubkey_b64, std::string* error) {
  if (key_id.empty()) {
    if (error) *error = "empty key_id";
    return false;
  }

  // If it already exists, just export its public key.
  {
    auto existing = find_private_key(key_id);
    if (existing) {
      return copy_external_pubkey_b64(existing.get(), pubkey_b64, error);
    }
  }

  auto tag = make_tag(key_id);
  if (!tag) {
    if (error) *error = "unable to build key tag";
    return false;
  }

  int bits = 256;
  CFRef<CFNumberRef> bits_num((CFNumberRef)CFNumberCreate(kCFAllocatorDefault, kCFNumberIntType, &bits));
  CFRef<CFMutableDictionaryRef> priv_attrs((CFMutableDictionaryRef)CFDictionaryCreateMutable(
      kCFAllocatorDefault, 0, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks));
  CFRef<CFMutableDictionaryRef> attrs((CFMutableDictionaryRef)CFDictionaryCreateMutable(
      kCFAllocatorDefault, 0, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks));
  if (!attrs || !priv_attrs || !bits_num) {
    if (error) *error = "out of memory";
    return false;
  }

  CFDictionarySetValue(attrs.get(), kSecAttrKeyType, kSecAttrKeyTypeECSECPrimeRandom);
  CFDictionarySetValue(attrs.get(), kSecAttrKeySizeInBits, bits_num.get());

  CFDictionarySetValue(priv_attrs.get(), kSecAttrIsPermanent, kCFBooleanTrue);
  CFDictionarySetValue(priv_attrs.get(), kSecAttrApplicationTag, tag.get());
  // Helpful for debugging in Keychain Access.
  CFRef<CFStringRef> label((CFStringRef)CFStringCreateWithCString(
      kCFAllocatorDefault, ("seq action-pack " + std::string(key_id)).c_str(), kCFStringEncodingUTF8));
  if (label) {
    CFDictionarySetValue(priv_attrs.get(), kSecAttrLabel, label.get());
  }
  CFDictionarySetValue(attrs.get(), kSecPrivateKeyAttrs, priv_attrs.get());

  CFErrorRef cf_err = nullptr;
  SecKeyRef priv = SecKeyCreateRandomKey(attrs.get(), &cf_err);
  if (!priv) {
    if (error) {
      std::string msg = cf_error_to_string(cf_err);
      *error = msg.empty() ? "keygen failed" : msg;
    }
    if (cf_err) CFRelease(cf_err);
    return false;
  }
  if (cf_err) CFRelease(cf_err);
  CFRef<SecKeyRef> priv_ref(priv);

  return copy_external_pubkey_b64(priv_ref.get(), pubkey_b64, error);
}

bool export_pubkey_p256(std::string_view key_id, std::string* pubkey_b64, std::string* error) {
  auto priv = find_private_key(key_id);
  if (!priv) {
    if (error) *error = "private key not found in keychain";
    return false;
  }
  return copy_external_pubkey_b64(priv.get(), pubkey_b64, error);
}

bool sign_p256(std::string_view key_id,
               std::string_view payload,
               std::vector<uint8_t>* signature,
               std::string* error) {
  if (!signature) return false;
  signature->clear();
  auto priv = find_private_key(key_id);
  if (!priv) {
    if (error) *error = "signing key not found (run: seq action-pack keygen)";
    return false;
  }

  CFRef<CFDataRef> data((CFDataRef)CFDataCreate(kCFAllocatorDefault,
                                                reinterpret_cast<const UInt8*>(payload.data()),
                                                (CFIndex)payload.size()));
  if (!data) {
    if (error) *error = "unable to allocate payload CFData";
    return false;
  }
  CFErrorRef cf_err = nullptr;
  CFDataRef sig = SecKeyCreateSignature(priv.get(),
                                        kSecKeyAlgorithmECDSASignatureMessageX962SHA256,
                                        data.get(),
                                        &cf_err);
  if (!sig) {
    if (error) {
      std::string msg = cf_error_to_string(cf_err);
      *error = msg.empty() ? "signature failed" : msg;
    }
    if (cf_err) CFRelease(cf_err);
    return false;
  }
  if (cf_err) CFRelease(cf_err);
  CFRef<CFDataRef> sig_ref(sig);
  const UInt8* bytes = CFDataGetBytePtr(sig_ref.get());
  CFIndex n = CFDataGetLength(sig_ref.get());
  if (!bytes || n <= 0) {
    if (error) *error = "empty signature";
    return false;
  }
  signature->assign(bytes, bytes + n);
  return true;
}

bool verify_p256(std::string_view pubkey_external_b64,
                 std::string_view payload,
                 std::string_view signature,
                 std::string* error) {
  std::vector<uint8_t> pub_ext;
  if (!base64::decode(pubkey_external_b64, &pub_ext) || pub_ext.empty()) {
    if (error) *error = "invalid pubkey base64";
    return false;
  }
  auto pub = create_public_key_from_external(pub_ext);
  if (!pub) {
    if (error) *error = "unable to import public key";
    return false;
  }

  CFRef<CFDataRef> data((CFDataRef)CFDataCreate(kCFAllocatorDefault,
                                                reinterpret_cast<const UInt8*>(payload.data()),
                                                (CFIndex)payload.size()));
  CFRef<CFDataRef> sig((CFDataRef)CFDataCreate(kCFAllocatorDefault,
                                               reinterpret_cast<const UInt8*>(signature.data()),
                                               (CFIndex)signature.size()));
  if (!data || !sig) {
    if (error) *error = "out of memory";
    return false;
  }

  CFErrorRef cf_err = nullptr;
  Boolean ok = SecKeyVerifySignature(pub.get(),
                                     kSecKeyAlgorithmECDSASignatureMessageX962SHA256,
                                     data.get(),
                                     sig.get(),
                                     &cf_err);
  if (!ok) {
    if (error) {
      std::string msg = cf_error_to_string(cf_err);
      *error = msg.empty() ? "signature invalid" : msg;
    }
    if (cf_err) CFRelease(cf_err);
    return false;
  }
  if (cf_err) CFRelease(cf_err);
  return true;
}

}  // namespace action_pack_crypto
