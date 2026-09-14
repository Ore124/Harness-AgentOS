"""Content-addressed artifact storage ports."""
from __future__ import annotations

import hashlib
import io
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Protocol


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    uri: str
    sha256: str
    size: int
    media_type: str = "application/octet-stream"
    sensitivity: str = "internal"
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArtifactStore(Protocol):
    def put(
        self,
        stream: BinaryIO | bytes,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord: ...
    def open(self, artifact: ArtifactRecord) -> BinaryIO: ...
    def signed_url(self, artifact: ArtifactRecord, expires_seconds: int = 300) -> str: ...


class LocalArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(
        self,
        stream: BinaryIO | bytes,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord:
        metadata = dict(metadata or {})
        source = io.BytesIO(stream) if isinstance(stream, bytes) else stream
        digest = hashlib.sha256()
        temporary = self.root / f".{uuid.uuid4().hex}.tmp"
        size = 0
        try:
            with temporary.open("wb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                    target.write(chunk)
            sha256 = digest.hexdigest()
            destination = self.root / sha256[:2] / sha256[2:4] / sha256
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                temporary.unlink()
            else:
                os.replace(temporary, destination)
            return ArtifactRecord(
                id=str(metadata.get("id") or uuid.uuid4()),
                uri=destination.as_uri(),
                sha256=sha256,
                size=size,
                media_type=str(metadata.get("media_type", "application/octet-stream")),
                sensitivity=str(metadata.get("sensitivity", "internal")),
                name=str(metadata["name"]) if metadata.get("name") else None,
            )
        finally:
            if temporary.exists():
                temporary.unlink()

    def open(self, artifact: ArtifactRecord) -> BinaryIO:
        path = _local_path(artifact.uri)
        if not path.is_relative_to(self.root):
            raise ValueError("artifact URI is outside this store")
        if _hash_file(path) != artifact.sha256:
            raise ValueError("artifact checksum mismatch")
        return path.open("rb")

    def signed_url(self, artifact: ArtifactRecord, expires_seconds: int = 300) -> str:
        del expires_seconds
        path = _local_path(artifact.uri)
        if not path.is_relative_to(self.root):
            raise ValueError("artifact URI is outside this store")
        return artifact.uri


class S3ArtifactStore:
    """S3-compatible implementation; boto3 is imported only when configured."""

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "harness-artifacts",
        endpoint_url: str | None = None,
        public_endpoint_url: str | None = None,
        client=None,
        signing_client=None,
    ):
        if client is None:
            try:
                import boto3
            except ImportError as exc:
                raise RuntimeError("boto3 is required for S3 artifact storage") from exc
            client = boto3.client("s3", endpoint_url=endpoint_url)
            if public_endpoint_url:
                signing_client = boto3.client("s3", endpoint_url=public_endpoint_url)
        self.client = client
        self.signing_client = signing_client or client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def put(
        self,
        stream: BinaryIO | bytes,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord:
        metadata = dict(metadata or {})
        payload = stream if isinstance(stream, bytes) else stream.read()
        sha256 = hashlib.sha256(payload).hexdigest()
        key = f"{self.prefix}/{sha256[:2]}/{sha256}"
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=payload,
            ContentType=str(metadata.get("media_type", "application/octet-stream")),
            Metadata={"sha256": sha256, "sensitivity": str(metadata.get("sensitivity", "internal"))},
        )
        return ArtifactRecord(
            id=str(metadata.get("id") or uuid.uuid4()),
            uri=f"s3://{self.bucket}/{key}",
            sha256=sha256,
            size=len(payload),
            media_type=str(metadata.get("media_type", "application/octet-stream")),
            sensitivity=str(metadata.get("sensitivity", "internal")),
            name=str(metadata["name"]) if metadata.get("name") else None,
        )

    def open(self, artifact: ArtifactRecord) -> BinaryIO:
        bucket, key = _split_s3_uri(artifact.uri)
        response = self.client.get_object(Bucket=bucket, Key=key)
        payload = response["Body"].read()
        if hashlib.sha256(payload).hexdigest() != artifact.sha256:
            raise ValueError("artifact checksum mismatch")
        return io.BytesIO(payload)

    def signed_url(self, artifact: ArtifactRecord, expires_seconds: int = 300) -> str:
        bucket, key = _split_s3_uri(artifact.uri)
        return self.signing_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )


def _local_path(uri: str) -> Path:
    from urllib.parse import unquote, urlparse

    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError("expected a local file artifact URI")
    path = unquote(parsed.path)
    if os.name == "nt" and path.startswith("/"):
        path = path[1:]
    return Path(path).resolve()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _split_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError("expected an s3:// artifact URI")
    bucket, separator, key = uri[5:].partition("/")
    if not bucket or not separator or not key:
        raise ValueError("invalid S3 artifact URI")
    return bucket, key
