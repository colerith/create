import base64
import binascii
import io
import json
import os
import subprocess
import struct
import unittest
import zipfile
import zlib

from cogs.protection.utils import (
    CARD_TRACE_KEY,
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
    def test_scoped_regex_and_other_extensions_are_preserved(self):
        scripts = [{
            'id': 'regex-test', 'scriptName': '局部正则',
            'findRegex': r'/<status>([\s\S]*?)<\/status>/g',
            'replaceString': '<div>$1</div>', 'trimStrings': [],
            'placement': [2], 'disabled': False, 'markdownOnly': True,
            'promptOnly': False, 'runOnEdit': True, 'substituteRegex': 0,
            'minDepth': None, 'maxDepth': None,
        }]
        for version, key in [('2.0', b'chara'), ('3.0', b'ccv3')]:
            card = {'spec': f'chara_card_v{version[0]}', 'spec_version': version,
                    'data': {'name': '测试角色', 'extensions': {
                        'regex_scripts': scripts, 'unknown_extension': {'enabled': True}},
                        'character_book': {'entries': [], 'extensions': {}}}}
            raw = json.dumps(card, ensure_ascii=False).encode('utf-8')
            marked = inject_smart_trace(png((key,), raw), 'card.png', TRACE)
            payload = next(p for _, _, k, p in _png_chunks(marked)
                           if k == b'tEXt' and p.startswith(key + b'\0'))
            decoded = base64.b64decode(payload.split(b'\0', 1)[1])
            parsed = json.loads(decoded)
            self.assertEqual(parsed['data']['extensions'].pop(CARD_TRACE_KEY),
                             {'version': 1, 'id': TRACE})
            self.assertEqual(parsed, card)

            # Optional integration check against an installed SillyTavern parser.
            parser = os.environ.get('SILLYTAVERN_CARD_PARSER')
            if parser:
                result = subprocess.run([
                    'node', '--input-type=module', '-e',
                    "import fs from 'node:fs'; import {pathToFileURL} from 'node:url';"
                    "const p=await import(pathToFileURL(process.argv[1]).href);"
                    "const input=fs.readFileSync(0);"
                    "const parsed=JSON.parse(p.read(input));"
                    "const exported=p.write(input,JSON.stringify(parsed));"
                    "process.stdout.write(JSON.stringify(JSON.parse(p.read(exported))));",
                    parser,
                ], input=marked, capture_output=True, check=True)
                exported_card = json.loads(result.stdout)
                self.assertEqual(extract_trace_from_bytes(result.stdout, 'card.json'), TRACE)
                exported_card['data']['extensions'].pop(CARD_TRACE_KEY)
                self.assertEqual(exported_card['data'], card['data'])

    def test_persistent_marker_survives_edit_and_reserialization(self):
        card = {'spec': 'chara_card_v3', 'spec_version': '3.0',
                'data': {'name': 'Original', 'extensions': {'regex_scripts': []}}}
        raw = json.dumps(card).encode()
        marked = _inject_png_card_trace(png(raw=raw), TRACE)
        marked = _inject_png_card_trace(marked, 'abcdef012345')
        for _, _, kind, payload in _png_chunks(marked):
            if kind != b'tEXt':
                continue
            decoded = json.loads(base64.b64decode(payload.split(b'\0', 1)[1]))
            decoded['data']['name'] = 'Edited'
            rewritten = json.dumps(decoded).encode()
            self.assertEqual(extract_trace_from_bytes(png(raw=rewritten), 'card.png'), 'abcdef012345')
            self.assertEqual(extract_trace_from_bytes(rewritten, 'card.json'), 'abcdef012345')

    def test_legacy_whitespace_still_readable_and_invalid_field_ignored(self):
        card = {'spec': 'chara_card_v2', 'data': {'extensions': {
            CARD_TRACE_KEY: {'version': 1, 'id': 'not-a-trace'}}}}
        raw = json.dumps(card).encode()
        self.assertIsNone(extract_trace_from_bytes(png(raw=raw), 'card.png'))
        self.assertEqual(extract_trace_from_bytes(
            png(raw=raw + _encode_trace_to_ws(TRACE)), 'card.png'), TRACE)

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
