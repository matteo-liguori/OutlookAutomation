from monitor.configuration import resolve
"""Export verified message attachments into the configured private data folder."""
import hashlib
from pathlib import Path

from .operations import native_operation


def download_attachment(mailbox,inbox_id,source_folder_id,entry_id,attachment,*,private_root,target=None,allow_large=False,runner=native_operation):
    root=Path(private_root).resolve()
    if target is None:
        raise ValueError('Provide a harness-allocated action-owned target')
    size=attachment.get('size')
    if size is not None and size>resolve().get('maintenance.small_attachment_bytes') and not allow_large:
        raise ValueError('Large attachment requires explicit DownloadLargeAttachments')
    from monitor.threadcache import checked_path
    target=checked_path(root,target)
    target.parent.mkdir(parents=True,exist_ok=True)
    result=runner(dict(action='attachment-save',authorized=True,mailbox=mailbox,folder_id=str(inbox_id),source_folder_id=str(source_folder_id),object_id=str(entry_id),attachment_index=attachment['index'],attachment_name=attachment['name'],destination=str(target)))
    if result.get('status')!='ok':return result
    if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(root):
        raise ValueError('Native attachment export did not produce a regular private file')
    actual=target.stat().st_size
    if actual>resolve().get('maintenance.small_attachment_bytes') and not allow_large:
        target.unlink();raise ValueError('Native attachment exceeds the download limit')
    return dict(status='ok',name=attachment['name'],index=attachment['index'],path=str(target),bytes=actual,
                reported_size=size,
                sha256=hashlib.sha256(target.read_bytes()).hexdigest(),read_state_preserved=result.get('read_state_preserved') is True)
