"""Project-scoped credentials in macOS Keychain, without secret CLI arguments."""
import ctypes
import hashlib
import sys
import threading
from pathlib import Path

PROVIDERS = {'doubao': '豆包 O2', 'openai': 'OpenAI Realtime'}
ENV_KEYS = {'doubao': 'DOUBAO_API_KEY', 'openai': 'OPENAI_API_KEY'}


def validate_provider(provider):
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError('请选择豆包 O2 或 OpenAI Realtime')
    return provider


def validate_key(key):
    if not isinstance(key, str) or not 1 <= len(key.strip()) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in key.strip()):
        raise ValueError('请输入有效的 API Key')
    return key.strip()


class MacKeychain:
    def __init__(self, root):
        if sys.platform != 'darwin':
            raise RuntimeError('当前密钥保存功能需要 macOS 钥匙串')
        digest = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:20]
        self.service = ('ESP32 Phone Console.' + digest).encode()
        self.lock = threading.Lock()
        self.security = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        self.core = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        pointer, uint, chars = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p
        self.security.SecKeychainFindGenericPassword.argtypes = [pointer, uint, chars, uint, chars,
            ctypes.POINTER(uint), ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
        self.security.SecKeychainAddGenericPassword.argtypes = [pointer, uint, chars, uint, chars, uint, pointer, ctypes.POINTER(pointer)]
        self.security.SecKeychainItemModifyAttributesAndData.argtypes = [pointer, pointer, uint, pointer]
        self.security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
        self.security.SecKeychainItemDelete.argtypes = [pointer]
        for name in ('SecKeychainFindGenericPassword', 'SecKeychainAddGenericPassword',
                     'SecKeychainItemModifyAttributesAndData', 'SecKeychainItemFreeContent', 'SecKeychainItemDelete'):
            getattr(self.security, name).restype = ctypes.c_int32
        self.core.CFRelease.argtypes = [pointer]
        self.core.CFRelease.restype = None

    @staticmethod
    def _check(status):
        if status:
            raise RuntimeError(f'钥匙串访问未完成（{status}），请检查系统授权或解锁登录钥匙串')

    def _find(self, provider, read=False):
        account = validate_provider(provider).encode()
        length, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(None, len(self.service), self.service,
            len(account), account, ctypes.byref(length) if read else None,
            ctypes.byref(data) if read else None, ctypes.byref(item))
        if status == -25300:
            return '', item
        self._check(status)
        try:
            value = ctypes.string_at(data, length.value).decode('utf-8') if read else ''
        except Exception:
            if item.value:
                self.core.CFRelease(item)
            raise RuntimeError('钥匙串中的配置无法读取，请重新保存该渠道 Key') from None
        finally:
            if data.value:
                self.security.SecKeychainItemFreeContent(None, data)
        return value, item

    def get(self, provider):
        with self.lock:
            value, item = self._find(provider, read=True)
            if item.value:
                self.core.CFRelease(item)
            return value

    def has(self, provider):
        with self.lock:
            _, item = self._find(provider)
            found = bool(item.value)
            if found:
                self.core.CFRelease(item)
            return found

    def set(self, provider, key):
        key = validate_key(key).encode()
        account = validate_provider(provider).encode()
        with self.lock:
            _, item = self._find(provider)
            try:
                if item.value:
                    status = self.security.SecKeychainItemModifyAttributesAndData(item, None, len(key), key)
                else:
                    status = self.security.SecKeychainAddGenericPassword(None, len(self.service), self.service,
                        len(account), account, len(key), key, None)
                self._check(status)
            finally:
                if item.value:
                    self.core.CFRelease(item)

    def delete(self, provider):
        with self.lock:
            _, item = self._find(provider)
            if item.value:
                try:
                    self._check(self.security.SecKeychainItemDelete(item))
                finally:
                    self.core.CFRelease(item)
