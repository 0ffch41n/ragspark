"""Stream one signed release manifest, its signature and its code artifact."""
import hashlib
import struct

from . import release as r, release_verify as v, update_check as check, update_host as host
from . import update_source as source


MAGIC = b'RSP1'
HEADER = struct.Struct('!4sII')


def metadata_sizes(manifest_size, signature_size):
    r.integer(manifest_size, 1, r.MAX_MANIFEST)
    r.integer(signature_size, 1, v.MAX_SIGNATURE)


def exact(stream, size):
    data = stream.read(size)
    r.need(len(data) == size, 'truncated pair bundle')
    return data


def copy_code(stream, out, artifact):
    digest = hashlib.sha256()
    remaining = artifact['bytes']
    while remaining:
        block = exact(stream, min(remaining, v.CHUNK))
        remaining -= len(block)
        digest.update(block)
        out.write(block)
    r.need(digest.hexdigest() == artifact['sha256'], 'pair code digest mismatch')
    r.need(not stream.read(1), 'unexpected data after the pair code artifact')


def send(stream, directory, receipt):
    """Write a verified local code archive. Close the stream after the frame."""
    raw, signature = receipt
    metadata_sizes(len(raw), len(signature))
    code = check.code_artifact(r.manifest(raw))
    v.verify_artifact(directory, code)
    with v.regular_file(directory / code['path']) as archive:
        stream.write(HEADER.pack(MAGIC, len(raw), len(signature)))
        stream.write(raw)
        stream.write(signature)
        copy_code(archive, stream, code)


def receive(stream, directory, trust, expected_digest):
    """Check the framing and authenticate the manifest before accepting code."""
    magic, manifest_size, signature_size = HEADER.unpack(exact(stream, HEADER.size))
    r.need(magic == MAGIC, 'unsupported pair bundle format')
    metadata_sizes(manifest_size, signature_size)
    raw = exact(stream, manifest_size)
    r.need(hashlib.sha256(raw).hexdigest() == r.digest(expected_digest), 'pair release digest differs')
    signature = exact(stream, signature_size)
    key, revoked = source.signing_paths(trust)
    v.verify_signature(raw, signature, key, 'ragspark-release-v1', revoked)
    code = check.code_artifact(r.manifest(raw))
    host.Space().add(directory, manifest_size + signature_size + code['bytes'])
    archive = directory / code['path']
    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open('xb') as out:
        copy_code(stream, out, code)
    (directory / 'release.json').write_bytes(raw)
    (directory / 'release.json.sig').write_bytes(signature)
