import base64
import binascii
import io
import json
import struct
import unittest
import zipfile
import zlib

from cogs.protection.utils import (
    _encode_trace_to_ws, _inject_png_card_trace, _png_chunks,
    extract_trace_from_bytes, inject_smart_trace,
)


TRACE = '012345abcdef'
RAW = '{ "name": "角色😀", "description": "测试", "extensions": {} }\n'.encode('utf-8')


def chunk(kind, payload):
    return (struct.pack('!I', len(payload)) + kind + payload
            + struct.pack('!I', binascii.crc32(kind + payload) & 0xffffffff))


def png(keys=(b'chara', b'ccv3'), raw=RAW):
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('!IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + b''.join(chunk(b'tEXt', key + b'\0' + base64.b64encode(raw)) for key in keys)
            + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0')) + chunk(b'IEND', b''))


def without_software(data):
    return data[:8] + b''.join(
        data[start:end] for start, end, kind, payload in _png_chunks(data)
        if not (kind == b'tEXt' and payload.startswith(b'Software\0')))


class PngTraceTests(unittest.TestCase):
    def test_card_versions_preserve_json_bytes_and_image(self):
        for keys in [(b'chara',), (b'ccv3',), (b'chara', b'ccv3')]:
            with self.subTest(keys=keys):
                original = png(keys)
                marked = inject_smart_trace(original, 'card.PNG', TRACE)
                cleaned = without_software(marked)
                self.assertEqual(extract_trace_from_bytes(cleaned, 'card.png'), TRACE)
                for _, _, kind, payload in _png_chunks(cleaned):
                    if kind == b'tEXt':
                        raw = base64.b64decode(payload.split(b'\0', 1)[1])
                        self.assertEqual(raw, RAW + _encode_trace_to_ws(TRACE))
                        self.assertEqual(json.loads(raw), json.loads(RAW))
                        self.assertEqual(extract_trace_from_bytes(raw, 'card.json'), TRACE)
                image = lambda data: [(k, p) for _, _, k, p in _png_chunks(data) if k != b'tEXt']
                self.assertEqual(image(original), image(marked))

    def test_reinjection_replaces_inner_marker(self):
        first = inject_smart_trace(png(), 'card.png', TRACE)
        second_id = 'abcdef012345'
        second = inject_smart_trace(first, 'card.png', second_id)
        self.assertEqual(extract_trace_from_bytes(second, 'card.png'), second_id)
        for _, _, kind, payload in _png_chunks(without_software(second)):
            if kind == b'tEXt':
                self.assertEqual(base64.b64decode(payload.split(b'\0', 1)[1]),
                                 RAW + _encode_trace_to_ws(second_id))

    def test_non_card_and_invalid_json_keep_legacy_trace(self):
        for original in [png(()), png(raw=b'not JSON'), png(raw=b'[]')]:
            self.assertEqual(_inject_png_card_trace(original, TRACE), original)
            self.assertEqual(extract_trace_from_bytes(
                inject_smart_trace(original, 'image.png', TRACE), 'image.png'), TRACE)

    def test_invalid_png_is_not_rewritten_by_card_injector(self):
        damaged = bytearray(png())
        damaged[29] ^= 1
        for original in [b'not PNG', png()[:-5], bytes(damaged)]:
            self.assertEqual(_inject_png_card_trace(original, TRACE), original)

    def test_zip_inner_card_is_marked(self):
        source = io.BytesIO()
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('card.png', png())
        marked = inject_smart_trace(source.getvalue(), 'cards.zip', TRACE)
        with zipfile.ZipFile(io.BytesIO(marked)) as archive:
            self.assertEqual(extract_trace_from_bytes(
                without_software(archive.read('card.png')), 'card.png'), TRACE)

    def test_json_reserialization_documents_limitation(self):
        marked = _inject_png_card_trace(png((b'chara',)), TRACE)
        payload = next(p for _, _, k, p in _png_chunks(marked) if k == b'tEXt')
        raw = base64.b64decode(payload.split(b'\0', 1)[1])
        rewritten = json.dumps(json.loads(raw)).encode()
        self.assertIsNone(extract_trace_from_bytes(png(raw=rewritten), 'card.png'))


if __name__ == '__main__':
    unittest.main()
