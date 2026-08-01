"""Showing the same picture twice is ordinary Markdown.

A page doing it passed the dry run and then failed the publish with an internal
error -- the worst shape a refusal can take, because the caller has already been
told it would work. Found by the live grid on a real operational page.

The cause was one binding per asset `src` being required. The journal entry
describes an upload, which happens once however many times the document points at
it, so the entry attaches to the first reference and the rest stay plain.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from atlassian_skills.confluence.models import Attachment
from atlassian_skills.confluence.pull_md import pull_md
from atlassian_skills.confluence.push_md import push_md

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.unit.test_state_free_body_write import BodyClient  # noqa: E402

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)
IMAGE = '<p><ac:image><ri:attachment ri:filename="diagram.png" /></ac:image></p>'
TWICE = f"<p>alpha paragraph text here</p>{IMAGE}<p>bravo text here</p>{IMAGE}"


class AttachedClient(BodyClient):
    def __init__(self) -> None:
        super().__init__()
        self.attachment_version = 1

    def list_attachments(self, page_id: str, limit: int | None = None) -> list:
        return [
            Attachment.model_validate(
                {
                    "id": "att1",
                    "title": "diagram.png",
                    "version": {"number": self.attachment_version},
                    "extensions": {"mediaType": "image/png", "fileSize": len(PNG)},
                    "_links": {"download": "/download/attachments/123/diagram.png"},
                }
            )
        ]

    def fetch_attachment_bytes(self, *args: object, **kwargs: object) -> bytes:
        return PNG

    def download_attachment(self, *args: object, **kwargs: object) -> bytes:
        return PNG

    def upload_attachment(self, page_id, path, *, filename=None, **kwargs):
        self.attachment_version += 1
        return {"id": "att1", "title": filename, "version": {"number": self.attachment_version}}


def test_the_same_picture_twice_publishes_as_the_dry_run_said_it_would() -> None:
    """Both halves matter. A dry run that promises `ready_to_publish` and a push
    that raises an internal error is worse than a refusal, because the caller
    acted on the promise."""

    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        client = AttachedClient()
        client.storage = TWICE
        managed = work / "page.md"
        pull_md(
            client,
            "123",
            output_path=managed,
            portable=True,
            asset_dir=work / "assets",
            resolve_assets="sidecar",
        )
        assert managed.read_text(encoding="utf-8").count("](assets/diagram.png)") == 2

        managed.write_text(
            managed.read_text(encoding="utf-8").replace("alpha paragraph", "alpha edited"),
            encoding="utf-8",
        )
        assert (
            push_md(client, "123", managed.read_text(encoding="utf-8"), managed_path=managed, dry_run=True)["status"]
            == "ready_to_publish"
        )

        published = push_md(client, "123", managed.read_text(encoding="utf-8"), managed_path=managed)
        assert published["status"] in {"updated", "reconciled"}
        assert client.storage.count("diagram.png") == 2
