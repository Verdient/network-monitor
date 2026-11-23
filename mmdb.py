"""Read MaxMind DB (.mmdb) files, the format used by DB-IP's open GeoIP databases."""

from ipaddress import ip_address
import mmap
import struct


METADATA_MARKER = b'\xab\xcd\xefMaxMind.com'
# Metadata sits within the last 128 KiB of the file.
METADATA_WINDOW = 128 * 1024
DATA_SEPARATOR = 16
# Pointer sizes 1-3 add these offsets so that each size covers a fresh range.
POINTER_BASES = (0, 2048, 526336, 0)
DECODED_LIMIT = 8192


class InvalidDatabase(ValueError):
    pass


class Reader:
    def __init__(self, path):
        with open(path, 'rb') as file:
            try:
                self.buffer = mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)
            except ValueError as error:
                raise InvalidDatabase(str(error)) from None
        try:
            start = self.buffer.rfind(METADATA_MARKER, max(0, len(self.buffer) - METADATA_WINDOW))
            if start < 0:
                raise InvalidDatabase('missing MaxMind DB metadata')
            self.data_start = start + len(METADATA_MARKER)
            metadata, _ = self._decode(self.data_start)
            if not isinstance(metadata, dict):
                raise InvalidDatabase('invalid MaxMind DB metadata')
            self.metadata = metadata
            self.node_count = metadata.get('node_count')
            self.record_size = metadata.get('record_size')
            self.ip_version = metadata.get('ip_version')
            if not isinstance(self.node_count, int) or self.node_count <= 0:
                raise InvalidDatabase('invalid node count')
            if self.record_size not in (24, 28, 32) or self.ip_version not in (4, 6):
                raise InvalidDatabase('unsupported record size or IP version')
            self.node_bytes = self.record_size // 4
            tree_size = self.node_bytes * self.node_count
            if tree_size + DATA_SEPARATOR > start:
                raise InvalidDatabase('truncated search tree')
            self.data_start = tree_size + DATA_SEPARATOR
            self.data_end = start
            self.decoded = {}
            self.ipv4_start = self._ipv4_start()
        except (InvalidDatabase, IndexError, struct.error, UnicodeDecodeError, RecursionError) as error:
            self.buffer.close()
            raise InvalidDatabase(str(error) or 'corrupt MaxMind DB') from None

    @property
    def build_epoch(self):
        epoch = self.metadata.get('build_epoch')
        return epoch if isinstance(epoch, int) else 0

    @property
    def database_type(self):
        kind = self.metadata.get('database_type')
        return kind if isinstance(kind, str) else ''

    def close(self):
        self.buffer.close()

    def _ipv4_start(self):
        if self.ip_version == 4:
            return 0
        node = 0
        # IPv4 addresses live under ::/96 in an IPv6 tree.
        for _ in range(96):
            if node >= self.node_count:
                break
            node = self._record(node, 0)
        return node

    def _record(self, node, bit):
        offset = node * self.node_bytes
        data = self.buffer
        if self.record_size == 24:
            offset += bit * 3
            return int.from_bytes(data[offset:offset + 3], 'big')
        if self.record_size == 32:
            offset += bit * 4
            return int.from_bytes(data[offset:offset + 4], 'big')
        # 28-bit records share their high nibbles in the middle byte.
        middle = data[offset + 3]
        if bit:
            return ((middle & 0x0F) << 24) | int.from_bytes(data[offset + 4:offset + 7], 'big')
        return ((middle & 0xF0) << 20) | int.from_bytes(data[offset:offset + 3], 'big')

    def get(self, address):
        """Return the record for an address, or None when the database has none."""
        ip = ip_address(address)
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.version == 6 and self.ip_version == 4:
            return None
        bits = ip.max_prefixlen
        value = int(ip)
        node = self.ipv4_start if ip.version == 4 else 0
        for depth in range(bits):
            if node >= self.node_count:
                break
            node = self._record(node, (value >> (bits - 1 - depth)) & 1)
        if node <= self.node_count:
            return None
        offset = node - self.node_count - DATA_SEPARATOR + self.data_start
        if not self.data_start <= offset < self.data_end:
            raise InvalidDatabase('record points outside the data section')
        cached = self.decoded.get(offset)
        if cached is None:
            cached = self._decode(offset)[0]
            if len(self.decoded) >= DECODED_LIMIT:
                self.decoded.clear()
            self.decoded[offset] = cached
        return cached

    def _decode(self, offset, depth=0):
        if depth > 32:
            raise InvalidDatabase('data nested too deeply')
        data = self.buffer
        control = data[offset]
        offset += 1
        kind = control >> 5
        if kind == 1:
            size = (control >> 3) & 0x3
            length = size + 1
            raw = data[offset:offset + length]
            prefix = 0 if size == 3 else control & 0x7
            pointer = int.from_bytes(bytes([prefix]) + raw, 'big') + POINTER_BASES[size]
            value, _ = self._decode(self.data_start + pointer, depth + 1)
            return value, offset + length
        if kind == 0:
            kind = 7 + data[offset]
            offset += 1
        size = control & 0x1F
        if size >= 29:
            extra = size - 28
            size = (29, 285, 65821)[extra - 1] + int.from_bytes(data[offset:offset + extra], 'big')
            offset += extra
        end = offset + size
        if kind == 2:
            return data[offset:end].decode('utf-8'), end
        if kind == 7:
            result = {}
            for _ in range(size):
                key, offset = self._decode(offset, depth + 1)
                result[key], offset = self._decode(offset, depth + 1)
            return result, offset
        if kind == 11:
            items = []
            for _ in range(size):
                item, offset = self._decode(offset, depth + 1)
                items.append(item)
            return items, offset
        if kind in (5, 6, 9, 10):
            return int.from_bytes(data[offset:end], 'big'), end
        if kind == 8:
            return int.from_bytes(data[offset:end].rjust(4, b'\0'), 'big', signed=True), end
        if kind == 3:
            return struct.unpack('>d', data[offset:offset + 8])[0], offset + 8
        if kind == 15:
            return struct.unpack('>f', data[offset:offset + 4])[0], offset + 4
        if kind == 4:
            return bytes(data[offset:end]), end
        if kind == 14:
            return bool(size), offset
        raise InvalidDatabase('unsupported data type {}'.format(kind))
