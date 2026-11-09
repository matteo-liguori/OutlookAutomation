"""Export verified message attachments into the configured private data folder."""
import hashlib
from pathlib import Path
import re
import uuid

from .operations import native_operation


def download_attachment(mailbox,inbox_id,source_folder_id,entry_id,attachment,*,private_root,allow_large=False,runner=native_operation):
    root=Path(private_root).resolve()
    name=re.sub(r'[^A-Za-z0-9._ -]','_',Path(attachment['name'].replace('\\','/')).name).strip(' .')[:120] or 'attachment'
    size=attachment.get('size')
    if size is not None and size>10*1024*1024 and not allow_large:
        raise ValueError('Large attachment requires explicit DownloadLargeAttachments')
    folder=root/'downloads'/uuid.uuid4().hex
    if any(node.is_symlink() for node in (root/'downloads',root)):
        raise ValueError('Attachment destination may not be a link')
    folder.mkdir(parents=True,exist_ok=False)
    target=folder/name
    result=runner(dict(action='attachment-save',authorized=True,mailbox=mailbox,folder_id=str(inbox_id),source_folder_id=str(source_folder_id),object_id=str(entry_id),attachment_index=attachment['index'],attachment_name=attachment['name'],destination=str(target)))
    if result.get('status')!='ok':return result
    if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(root):
        raise ValueError('Native attachment export did not produce a regular private file')
    actual=target.stat().st_size
    if actual>10*1024*1024 and not allow_large:
        target.unlink();raise ValueError('Native attachment exceeds the download limit')
    return dict(status='ok',name=attachment['name'],index=attachment['index'],path=str(target),bytes=actual,
                reported_size=size,
                sha256=hashlib.sha256(target.read_bytes()).hexdigest(),read_state_preserved=result.get('read_state_preserved') is True)
