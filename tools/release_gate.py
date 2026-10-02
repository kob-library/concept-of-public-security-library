#!/usr/bin/env python3
"""Fail-closed publisher gate. The presence of authors' permissive notice alone does not approve distribution.

Source-document metrics in each volume's ``data/qa.json`` (embedded OLE count,
media Pandoc did not place inline) are historical facts about the DOC file and
must NOT change after review. Findings are closed through the human review
queues in ``review/``: an OLE or omitted image counts as resolved when its
queue entry reaches ``human_review_status == 'verified'``. The gate therefore
checks queue coverage of source findings instead of requiring the source
metrics to go to zero.

Content binding (audit finding F1): the approval file carries ``source_pins``
— SHA-256 of every source file the reviewer approved. The gate re-hashes the
bytes on disk and compares them to the pins; replacing a DOC/PDF or adding an
unpinned file after approval blocks the release. Verified queue media are
likewise re-hashed against the recorded ``source_file_sha256``. Reviewing the
pin values themselves is part of the human approval procedure (see
``review/README.md``): the gate checks consistency, not who wrote the file.
"""
import argparse, collections, hashlib, json
from pathlib import Path
BASE_REQUIRED=['editorial_text_review','doc_pdf_edition_check','jurisdiction_distribution_review','human_final_approval']
# release_scope.media: 'none' (text-first, no shipped graphics) or 'allowlist'
# (only files positively listed in review/public_media_allowlist.jsonl).
MEDIA_SCOPES=('none','allowlist')
# release_scope.pdfs: 'ship' (bundled, requires third-party-art review of
# embedded covers/attachments), 'reference' (not bundled; citations point at
# release_scope.canonical_pdf_url) or 'none' (no PDF reference at all).
PDF_SCOPES=('none','reference','ship')

def parse_release_scope(approval:dict,reasons:list):
    """The scope is the reviewer's declaration of the approved artifact
    composition. The gate validates its shape and derives which review flags
    it implies; it cannot grant any approval by itself."""
    scope=approval.get('release_scope')
    if not isinstance(scope,dict):
        reasons.append('release_scope missing or not an object {volumes, media, pdfs, ...}')
        return {'media':'none','pdfs':'none','volumes':[]}
    vols=scope.get('volumes')
    if not isinstance(vols,list) or not vols or len(set(vols))!=len(vols) or any(v not in (1,2,3,4,5,6) for v in vols):
        reasons.append('release_scope.volumes must list each approved volume once (1..6)')
    if scope.get('media') not in MEDIA_SCOPES:
        reasons.append("release_scope.media must be 'none' or 'allowlist'")
    if scope.get('pdfs') not in PDF_SCOPES:
        reasons.append("release_scope.pdfs must be 'none', 'reference' or 'ship'")
    if scope.get('pdfs')=='reference':
        cu=scope.get('canonical_pdf_url')
        if not isinstance(cu,str) or not cu.startswith('https://'):
            reasons.append('release_scope.canonical_pdf_url must be an https URL when pdfs=="reference"')
    return scope

def jsonl(path):
    return [json.loads(x) for x in path.read_text(encoding='utf8').splitlines() if x.strip()]

def sha256(path:Path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(2**20),b''):h.update(chunk)
    return h.hexdigest()

def check_source_pins(corpus:Path, approval:dict, reasons:list, notes:list):
    """Bind the release decision to the exact source bytes reviewed."""
    pins=approval.get('source_pins')
    if not isinstance(pins,dict):
        reasons.append('Approval file lacks source_pins (SHA-256 of reviewed DOC/PDF)')
        return
    for v in range(1,7):
        src=corpus/f'tom-{v}/source'
        pin=pins.get(f'tom-{v}')
        if not isinstance(pin,dict):
            reasons.append(f'Volume {v}: source pin absent in approval');continue
        expected={f'osnovy-sociologii-tom-{v}.doc':pin.get('doc_sha256'),
                  f'osnovy-sociologii-tom-{v}.pdf':pin.get('pdf_sha256')}
        expected.update(pin.get('extra_pdfs') or {})
        for name,want in expected.items():
            f=src/name
            if not f.is_file():
                reasons.append(f'Volume {v}: pinned source missing on disk: {name}');continue
            if not want:
                reasons.append(f'Volume {v}: pin value empty for {name}');continue
            if sha256(f)!=want:
                reasons.append(f'Volume {v}: source {name} sha256 differs from approved pin (content replaced after approval)')
        if src.is_dir():
            unpinned=sorted(f.name for f in src.iterdir() if f.is_file() and f.name not in expected)
            if unpinned:reasons.append(f'Volume {v}: unpinned files in source/: {", ".join(unpinned)}')
    notes.append('Source pins: exact DOC/PDF SHA-256 recorded in the approval file')

def check_verified_media_bytes(corpus:Path, media_rows:list, reasons:list):
    """A 'verified' queue entry must still describe the bytes on disk."""
    for r in media_rows:
        if r.get('human_review_status')!='verified':continue
        v,img=r.get('volume'),r.get('image')
        rel=r.get('source_original_path') or f'assets/media_original/{img}'
        f=corpus/f'tom-{v}'/rel
        want=r.get('source_file_sha256')
        if not f.is_file():
            reasons.append(f'Verified media missing from corpus: {img} (volume {v})');continue
        if not want:
            reasons.append(f'Verified media lacks recorded sha256: {img} (volume {v})');continue
        if sha256(f)!=want:
            reasons.append(f'Verified media sha256 mismatch: {img} (volume {v}) — file changed after review')

MEDIA_ALLOWLIST='public_media_allowlist.jsonl'

def check_public_media(corpus:Path, volumes, media_allowlist:Path, approval:dict,
                       verified_media:set, reasons:list, notes:list):
    """Positive immutable allowlist of published preview media.

    Every file that site_builder could ship (corpus tom-N/assets/media/**)
    must be covered by an allowlist entry: unique (volume, normalized
    'assets/media/<name>' path), decision='approved', named reviewer, scope
    equal to the approval's release_scope, sha256 of the published preview
    bytes, and a source_image that is itself verified in the review queue.
    Unknown or byte-changed preview files, duplicate/missing entries and
    out-of-sandbox paths all block the release.
    """
    previews={v:sorted(p for p in (corpus/f'tom-{v}/assets/media').rglob('*') if p.is_file())
              for v in volumes if (corpus/f'tom-{v}/assets/media').is_dir()}
    total=sum(len(x) for x in previews.values())
    if not total:
        notes.append('No publishable preview media present');return
    entries={}
    if not media_allowlist.is_file():
        reasons.append(f'Public media allowlist missing while {total} publishable preview files exist')
        return
    try:rows=jsonl(media_allowlist)
    except Exception:reasons.append('Public media allowlist unparseable');return
    scope=approval.get('release_scope')
    scope_name=scope.get('name') if isinstance(scope,dict) else scope
    for e in rows:
        v=e.get('volume');path=e.get('path')
        key=(v,path)
        if not isinstance(v,int) or not path or key in entries:
            reasons.append(f'Allowlist entry duplicate or malformed: {e!r}');continue
        if not path.startswith('assets/media/') or '..' in path or path.startswith('/'):
            reasons.append(f'Allowlist path out of media sandbox: {path}');continue
        if e.get('decision')!='approved':
            reasons.append(f'Allowlist entry not approved: {path} (volume {v})');continue
        if not e.get('reviewer'):
            reasons.append(f'Allowlist entry lacks reviewer: {path} (volume {v})');continue
        if e.get('scope')!=scope_name:
            reasons.append(f'Allowlist entry scope mismatch: {path} (volume {v})');continue
        src=e.get('source_image')
        if (v,src) not in verified_media:
            reasons.append(f'Allowlist entry {path} (volume {v}) does not reference a verified queue original: {src}');continue
        entries[key]=e
    covered=set()
    for v,files in previews.items():
        for f in files:
            rel=f'assets/media/{f.name}'
            e=entries.get((v,rel))
            if not e:
                reasons.append(f'Publishable media not allowlisted: tom-{v}/{rel}');continue
            covered.add((v,rel))
            if sha256(f)!=e.get('sha256'):
                reasons.append(f'Allowlisted media bytes differ from approved: tom-{v}/{rel}')
    for (v,rel) in entries:
        if (v,rel) not in covered:
            reasons.append(f'Allowlist entry references missing preview file: tom-{v}/{rel}')
    notes.append(f'Public media allowlist: {len(covered)} of {total} preview files approved')

def check_release(corpus:Path, release_file:Path):
    """Returns (approved, blocking_reasons, informational_notes)."""
    reasons=[];notes=[]
    if not release_file or not release_file.is_file():return False,['Missing explicit approval file'],notes
    try:approval=json.loads(release_file.read_text(encoding='utf8'))
    except Exception:return False,['Unparseable approval file'],notes
    scope=parse_release_scope(approval,reasons)
    volumes=sorted(scope.get('volumes') or [])
    media_scope=scope.get('media');pdf_scope=scope.get('pdfs')
    required=list(BASE_REQUIRED)
    # Review flags follow the declared artifact composition, not the other way
    # round: media/OLE/art queues gate only releases that actually ship them.
    if media_scope!='none':required.append('illustrations_and_ole_review')
    if media_scope!='none' or pdf_scope=='ship':required.append('rights_and_third_party_art_review')
    for label in required:
        if approval.get(label) is not True:reasons.append('Not approved: '+label)
    if not approval.get('approved_by') or not approval.get('approval_date'):
        reasons.append('Reviewer identity/date missing')
    check_source_pins(corpus,approval,reasons,notes)
    media_rows=[];ole_rows=[]
    for path,label,store in [(release_file.parent/'media_review_queue.jsonl','Image',media_rows),
                           (release_file.parent/'ole_review_queue.jsonl','OLE',ole_rows)]:
        if not path.is_file():
            if media_scope!='none':reasons.append(label+' QA queue missing')
            continue
        rows=jsonl(path)
        store.extend(rows)
        if not rows:continue
        pending=[x['id'] if 'id' in x else x.get('saved_as','unknown') for x in rows if x.get('human_review_status')!='verified']
        if pending:
            msg=f'{label}: {len(pending)} review entries not human-verified'
            (reasons if media_scope!='none' else notes).append(msg+('' if media_scope!='none' else ' (not shipped in this scope)'))
    check_verified_media_bytes(corpus,media_rows,reasons)
    verified_media={(r.get('volume'),r.get('image')) for r in media_rows if r.get('human_review_status')=='verified'}
    if media_scope=='allowlist':
        check_public_media(corpus,volumes,release_file.parent/MEDIA_ALLOWLIST,approval,verified_media,reasons,notes)
    elif media_scope=='none':
        notes.append('Media scope "none": no graphics shipped; media/OLE queues deferred')
    # One OLE object = one stable insertion identity (saved_as path). Duplicate
    # rows for the same identity must not inflate coverage; identical bytes at
    # different insertion points remain distinct objects.
    verified_ole=collections.Counter(v for v,_ in
        {(r.get('volume'),r.get('saved_as')) for r in ole_rows
         if r.get('human_review_status')=='verified' and r.get('saved_as')})
    for v in (volumes or range(1,7)):
        p=corpus/f'tom-{v}'
        if not (p/'data/sections.jsonl').exists():reasons.append(f'Volume {v}: missing processed text')
        if not (p/'source'/f'osnovy-sociologii-tom-{v}.pdf').exists():reasons.append(f'Volume {v}: missing source PDF')
        if v==1:
            # Pilot schema (v0.3): no qa.json; comparable facts live in
            # data/manifest.json. Deep checks run via converter/validate_tom1.py.
            q=p/'data/manifest.json'
            if not q.exists():reasons.append('Volume 1: pilot manifest missing');continue
            try:z=json.loads(q.read_text(encoding='utf8'))
            except Exception:reasons.append('Volume 1: pilot manifest unparseable');continue
            not_placed=z.get('docx_media_without_body_use') or []
            if not_placed:
                uncovered=[n for n in not_placed if (v,n) not in verified_media]
                if uncovered:
                    msg=f'Volume 1: {len(uncovered)} of {len(not_placed)} non-inline media lack a verified review entry (e.g. {", ".join(sorted(uncovered)[:3])})'
                    (reasons if media_scope!='none' else notes).append(msg)
                else:notes.append(f'Volume 1: {len(not_placed)} non-inline media covered by verified queue entries')
        else:
            q=p/'data/qa.json'
            if not q.exists():reasons.append(f'Volume {v}: QA missing');continue
            z=json.loads(q.read_text(encoding='utf8'))
            if not z.get('source_text_split_lossless'):reasons.append(f'Volume {v}: split text not lossless')
            # Images omitted from inline Markdown: a fixed fact of the source
            # DOC. Resolved by a verified queue entry per (volume, image),
            # not by editing qa.json.
            not_placed=z.get('docx_media_not_placed')
            omitted=z.get('omitted_inline_media_unique')
            if omitted is None:omitted=len(not_placed or [])
            if omitted:
                notes.append(f'Volume {v}: {omitted} source media not inline in Markdown (source metric, unchanged)')
            if not_placed:
                uncovered=[n for n in not_placed if (v,n) not in verified_media]
                if uncovered:
                    msg=f'Volume {v}: {len(uncovered)} of {len(not_placed)} omitted media lack a verified review entry (e.g. {", ".join(sorted(uncovered)[:3])})'
                    (reasons if media_scope!='none' else notes).append(msg)
            elif omitted:
                reasons.append(f'Volume {v}: {omitted} omitted media reported but docx_media_not_placed list absent; cannot match against review queue')
            # Embedded OLE stay inside the source DOC forever; resolved by a
            # verified OLE queue entry for each distinct object in this volume,
            # not by removal.
            ole_total=z.get('OLE_objects') or 0
            if ole_total:
                done=verified_ole.get(v,0)
                notes.append(f'Volume {v}: {ole_total} OLE objects in source DOC; {done} verified in queue')
                if done<ole_total:
                    msg=f'Volume {v}: {ole_total-done} of {ole_total} embedded OLE objects not human-verified'
                    (reasons if media_scope!='none' else notes).append(msg)
    return not reasons,reasons,notes
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--corpus',required=True,type=Path);p.add_argument('--approval',required=True,type=Path);a=p.parse_args()
    ok,issues,notes=check_release(a.corpus,a.approval)
    print(json.dumps({'approved':ok,'blocked_reasons':issues,'notes':notes},ensure_ascii=False,indent=2))
    if not ok:raise SystemExit(2)
