# SPDX-FileCopyrightText: 2021 Luke Granger-Brown <git@lukegb.com>
#
# SPDX-License-Identifier: MIT

import base64
import dataclasses
import enum
import hashlib
import io
import re
import sys
import textwrap
from typing import List, Optional, Protocol, TextIO, Tuple
import urllib.parse


def _decode_len(i: bytes) -> Tuple[int, bytes, bytes]:
    if i[0] > 0x80:
        length_length = i[0] & 0x7F
        return (
            int.from_bytes(i[1 : 1 + length_length], "big"),
            i[: 1 + length_length],
            i[1 + length_length :],
        )
    else:
        return i[0], i[:1], i[1:]


def _encode_len(n: int) -> bytearray:
    if n < 0x80:
        return bytearray([n])
    length_bytes = (n.bit_length() + 7) // 8
    assert length_bytes < 0x80
    out = bytearray([length_bytes | 0x80])
    out.extend(n.to_bytes(length_bytes, "big"))
    return out


def der_int_to_python(i: bytes) -> Tuple[int, bytes, bytes]:
    assert i[0] == 0x02
    length, length_bytes, data = _decode_len(i[1:])
    assert len(data) == length
    return (
        int.from_bytes(data, "big"),
        i[:1] + length_bytes + data[:length],
        data[length:],
    )


def _encode_int(n: int) -> bytearray:
    if n < 0x80:
        return bytearray([n])
    # Encode LSB-first
    nbuf = bytearray()
    while n > 0:
        nbuf.append((n & 0x7F) | 0x80)
        n >>= 7
    # Strip the marker flag off the least-significant-bit.
    nbuf[0] = nbuf[0] & 0x7F
    # Flip to MSB before output
    return nbuf[::-1]


def _decode_int(b: bytes) -> Tuple[int, bytes]:
    if b[0] < 0x80:
        return int(b[0]), b[1:]

    n = 0
    pos = 0
    while True:
        n = (n << 7) | (b[pos] & 0x7F)
        if (b[pos] & 0x80) == 0:
            return n, b[pos + 1 :]
        pos += 1


def _dumb_decode(i: bytes) -> Tuple[Tuple[int, bytes], bytes]:
    tag = i[0]
    der_len, length_bytes, buf = _decode_len(i[1:])
    return (tag, i[:1] + length_bytes + buf[:der_len]), buf[der_len:]


@dataclasses.dataclass(frozen=True, order=True)
class ObjectID:
    name: Optional[str]
    oid: List[int]

    def __str__(self) -> str:
        return ".".join(str(s) for s in self.oid)

    def __repr__(self) -> str:
        return str(self)

    def __eq__(self, other) -> bool:
        if isinstance(other, ObjectID):
            return self.oid == other.oid
        else:
            return False

    def __hash__(self) -> int:
        return hash(self.oid)

    @classmethod
    def from_str(cls, name: Optional[str], oid: str) -> "ObjectID":
        return cls(name=name, oid=[int(s) for s in oid.split(".")])

    def as_der(self) -> bytes:
        buf = bytearray()
        buf.append((40 * self.oid[0]) + self.oid[1])
        for n in self.oid[2:]:
            buf.extend(_encode_int(n))

        prefix = bytearray([0x06])
        prefix.extend(_encode_len(len(buf)))
        return bytes(prefix + buf)

    @classmethod
    def from_der(cls, b: bytes) -> Tuple["ObjectID", bytes]:
        assert b[0] == 0x06
        oid_len, _, b = _decode_len(b[1:])
        b, trailing = b[:oid_len], b[oid_len:]

        oid = []
        oid.append(min(b[0] // 40, 2))
        oid.append(b[0] - (oid[0] * 40))
        b = b[1:]
        while b:
            oid_seg, b = _decode_int(b)
            oid.append(oid_seg)

        return cls(name=None, oid=oid), trailing


@dataclasses.dataclass(frozen=True)
class ExtendedKeyUsages:
    oids: List[ObjectID]

    def as_der(self) -> bytes:
        buf = bytearray()
        for oid in self.oids:
            buf.extend(oid.as_der())

        prefix = bytearray([0x30])
        prefix.extend(_encode_len(len(buf)))
        return bytes(prefix + buf)


class DerSerializable(Protocol):
    def as_der(self) -> bytes:
        ...


@dataclasses.dataclass(frozen=True)
class CertExtension:
    ext_id: ObjectID
    critical: bool
    extension: DerSerializable

    def as_der(self) -> bytes:
        buf = bytearray()
        buf.extend(self.ext_id.as_der())

        if self.critical:
            # Bool, length one, true
            # Critical is optional, and the default is false, so in DER it's only present if it's true.
            buf.extend([0x01, 0x01, 0xFF])

        # We have to wrap the extension in an OCTET STRING
        extension_der = self.extension.as_der()
        buf.extend([0x04])
        buf.extend(_encode_int(len(extension_der)))
        buf.extend(extension_der)

        prefix = bytearray([0x30])
        prefix.extend(_encode_len(len(buf)))
        return bytes(prefix + buf)


@dataclasses.dataclass(frozen=True)
class TBSCertificate:
    serial_number: bytes
    signature: bytes
    issuer: bytes
    validity: bytes
    subject: bytes
    subject_public_key_info: bytes

    version: Optional[bytes] = None
    issuer_unique_id: Optional[bytes] = None
    subject_unique_id: Optional[bytes] = None
    extensions: Optional[bytes] = None

    @classmethod
    def from_der(cls, b: bytes) -> Tuple["TBSCertificate", bytes]:
        assert b[0] == 0x30
        cert_len, _, b = _decode_len(b[1:])
        b, rem = b[:cert_len], b[cert_len:]

        data = {}

        (tag, dec_bytes), b = _dumb_decode(b)
        if tag == 0xA0:
            data["version"] = dec_bytes

            (tag, dec_bytes), b = _dumb_decode(b)
        assert tag == 0x02
        data["serial_number"] = dec_bytes

        (tag, dec_bytes), b = _dumb_decode(b)
        assert tag == 0x30
        data["signature"] = dec_bytes

        (tag, dec_bytes), b = _dumb_decode(b)
        assert tag == 0x30
        data["issuer"] = dec_bytes

        (tag, dec_bytes), b = _dumb_decode(b)
        assert tag == 0x30
        data["validity"] = dec_bytes

        (tag, dec_bytes), b = _dumb_decode(b)
        assert tag == 0x30
        data["subject"] = dec_bytes

        (tag, dec_bytes), b = _dumb_decode(b)
        assert tag == 0x30
        data["subject_public_key_info"] = dec_bytes

        if b:
            (tag, dec_bytes), b = _dumb_decode(b)
            if tag == 0xA1:
                data["issuer_unique_id"] = dec_bytes
                if b:
                    (tag, dec_bytes), b = _dumb_decode(b)
            if tag == 0xA2:
                data["subject_unique_id"] = dec_bytes
                if b:
                    (tag, dec_bytes), b = _dumb_decode(b)
            if tag == 0xA3:
                data["extensions"] = dec_bytes
                if b:
                    (tag, dec_bytes), b = _dumb_decode(b)

        return cls(**data), rem


@dataclasses.dataclass(frozen=True)
class Certificate:
    tbs_certificate: TBSCertificate
    signature_algorithm: bytes
    signature_value: bytes

    @classmethod
    def from_der(cls, b: bytes) -> Tuple["Certificate", bytes]:
        assert b[0] == 0x30  # sequence
        cert_len, _, b = _decode_len(b[1:])
        b, rem = b[:cert_len], b[cert_len:]
        tbs_certificate, b = TBSCertificate.from_der(b)

        (tag, signature_algorithm), b = _dumb_decode(b)
        assert tag == 0x30

        (tag, signature_value), b = _dumb_decode(b)
        assert tag == 0x03

        assert not b

        return (
            cls(
                tbs_certificate=tbs_certificate,
                signature_algorithm=signature_algorithm,
                signature_value=signature_value,
            ),
            rem,
        )

    def public_key_pem(self) -> "PEMBlock":
        return PEMBlock(
            name="PUBLIC KEY", content=self.tbs_certificate.subject_public_key_info
        )


@dataclasses.dataclass(frozen=True)
class PEMBlock:
    name: str
    content: bytes

    def encode(self) -> str:
        encoded = "\n".join(
            textwrap.wrap(base64.b64encode(self.content).decode("utf-8"), 64)
        )
        return textwrap.dedent(
            """\
            -----BEGIN {name}-----
            {encoded}
            -----END {name}-----
        """
        ).format(name=self.name, encoded=encoded)

    @classmethod
    def decode(cls, b: str) -> Optional[Tuple[str, "PEMBlock", str]]:
        sio = io.StringIO(b)
        ret = cls.decode_from_file(sio)
        if not ret:
            return None
        return ret[0], ret[1], sio.read()

    @classmethod
    def decode_from_file(cls, fp: TextIO) -> Optional[Tuple[str, "PEMBlock"]]:
        prefix = []
        start_match = None
        for ln in fp:
            start_match = re.match(r"^-----BEGIN ([^-]+)-----$", ln)
            if start_match:
                break
            prefix.append(ln)
            continue
        if not start_match:
            return None
        name = start_match.group(1)

        bits = []
        for ln in fp:
            end_match = re.match(f"^-----END {re.escape(name)}-----$", ln)
            if end_match:
                break
            bits.append(ln)
            continue
        if not end_match:
            return None
        return "".join(prefix), cls(name=name, content=base64.b64decode("".join(bits)))


@dataclasses.dataclass(frozen=True)
class OpenSSLCertAux:
    trust: List[ObjectID]
    reject: List[ObjectID]

    @staticmethod
    def encode_oids(oids: List[ObjectID], tag: int = 0x30) -> bytes:
        buf = bytearray()
        for oid in oids:
            buf.extend(oid.as_der())
        prefix = bytearray([tag])
        prefix.extend(_encode_len(len(buf)))
        return bytes(prefix + buf)

    def as_der(self) -> bytes:
        buf = bytearray()
        buf.extend(self.encode_oids(self.trust, 0x30))
        if self.reject:
            buf.extend(self.encode_oids(self.reject, 0xA0))

        prefix = bytearray([0x30])
        prefix.extend(_encode_len(len(buf)))
        return bytes(prefix + buf)

    @classmethod
    def from_der(cls, b: bytes) -> Tuple["OpenSSLCertAux", bytes]:
        assert b[0] == 0x30  # sequence
        aux_len, _, b = _decode_len(b[1:])
        b, rem = b[:aux_len], b[aux_len:]

        assert b[0] == 0x30  # sequence, trust
        trust_len, _, b = _decode_len(b[1:])
        trust_b, b = b[:trust_len], b[trust_len:]
        trust = []
        while trust_b:
            trust_oid, trust_b = ObjectID.from_der(trust_b)
            trust.append(trust_oid)

        reject = []
        if b and b[0] == 0xA0:  # sequence, reject (a0 tag)
            reject_len, _, b = _decode_len(b[1:])
            reject_b, b = b[:reject_len], b[reject_len:]
            while reject_b:
                reject_oid, reject_b = ObjectID.from_der(reject_b)
                reject.append(reject_oid)

        return cls(trust=trust, reject=reject), rem


def to_trusted_certificate(cert: bytes, certaux: OpenSSLCertAux) -> PEMBlock:
    return PEMBlock(name="TRUSTED CERTIFICATE", content=cert + certaux.as_der())


def parse_trusted_certificate(pb: PEMBlock) -> Tuple[bytes, OpenSSLCertAux, bytes]:
    assert pb.name == "TRUSTED CERTIFICATE"
    # TODO(lukegb): make this less bad: we parse the cert then throw it away
    # this is because I don't want to ensure that we're parsing the cert "properly"
    cert, trailing = Certificate.from_der(pb.content)
    cert_bytes = pb.content[: len(pb.content) - len(trailing)]
    cert_aux, trailing = OpenSSLCertAux.from_der(trailing)
    return cert_bytes, cert_aux, trailing


@dataclasses.dataclass(frozen=True)
class DistinguishedName:
    bits: List[List[Tuple[ObjectID, str]]]

    @classmethod
    def from_der(cls, b: bytes) -> Tuple["DistinguishedName", bytes]:
        assert b[0] == 0x30
        dn_len, _, b = _decode_len(b[1:])
        b, rem = b[:dn_len], b[dn_len:]

        bits = []
        while b:
            assert b[0] == 0x31
            set_len, _, set_b = _decode_len(b[1:])
            set_b, b = set_b[:set_len], set_b[set_len:]

            set_bits = []
            while set_b:
                assert set_b[0] == 0x30
                seq_len, _, seq_b = _decode_len(set_b[1:])
                seq_b, set_b = seq_b[:seq_len], seq_b[seq_len:]

                seq_oid, seq_b = ObjectID.from_der(seq_b)
                print(seq_b[0])
                assert seq_b[0] in (0x13, 0x14, 0x0C, 0x16, 0x1E)
                part_len, _, seq_b = _decode_len(seq_b[1:])
                part_b, seq_b = seq_b[:seq_len], seq_b[seq_len:]
                assert seq_b == b"", "DN seq contained >2 parts"
                set_bits.append((seq_oid, part_b.decode("utf-8")))
            bits.append(set_bits)
        return cls(bits=bits), rem

    @staticmethod
    def _bit_to_str(bit: Tuple[ObjectID, str]) -> str:
        from . import x509_consts

        oid, s = bit
        if str(oid) in x509_consts.ATTRIBUTES:
            oid = x509_consts.ATTRIBUTES[str(oid)]
            return f"{oid.name}={s}"
        else:
            return f"{str(oid)}={s}"

    @classmethod
    def _set_to_str(cls, set_bits: List[Tuple[ObjectID, str]]) -> str:
        assert len(set_bits) > 0
        if len(set_bits) == 1:
            return cls._bit_to_str(set_bits[0])
        else:
            return "{%s}" % (",".join(cls._bit_to_str(sb) for sb in set_bits))

    def last_part(self) -> str:
        return self._set_to_str(self.bits[-1])

    def __str__(self) -> str:
        return ",".join(self._set_to_str(b) for b in self.bits)
