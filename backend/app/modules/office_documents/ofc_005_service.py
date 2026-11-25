"""OFC-005 — reviewed, scoped office draft versions and deterministic local exports.

No official ERP document, external messaging, printer dispatch or macro execution.
"""
from __future__ import annotations

from base64 import b64encode
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from hashlib import sha256
from html import escape as html_escape
from io import BytesIO
from typing import Literal
import json
import re
from uuid import uuid4
from xml.sax.saxutils import escape as xml_escape
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.idempotency import require_idempotency_key
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.models import Subject
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.identity.command_guard import lock_employee_authority
from backend.app.modules.office_intake.ofc_002_service import get_bytes as evidence_bytes, OfficeIntakeError
from backend.app.modules.office_documents.ofc_005_models import (
    OfficeDocumentVersion, OfficeDocumentReview, OfficeDocumentExport)
from backend.app.modules.office_documents.ofc_005_schemas import (
    DocumentCreate, DocumentDecision, DocumentExport, DocumentEnvelope)

def _prevent_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/office-documents", tags=["office-documents"],
                   dependencies=[Depends(_prevent_caching)])
_TEMPLATE = {"quotation":"office-quotation-v2", "reconciliation":"office-reconcile-v2",
             "receiving_list":"office-receiving-v2"}
_LEGACY_TEMPLATE = {key: value.replace("-v2", "-v1") for key, value in _TEMPLATE.items()}
_NUM = re.compile(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,3})?\Z")
_MONEY = re.compile(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,2})?\Z")
_SAFE_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,120}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
# XML 1.0 Char ranges, not a ban on all Unicode noncharacters.
_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_TWO = Decimal("0.01")


def _fail(code, status=409):
    raise HTTPException(status_code=status, detail=code)


def _utc(t):
    if not isinstance(t, datetime) or t.tzinfo is None or t.utcoffset() is None:
        _fail("office_document_time_invalid", 422)
    return t.astimezone(timezone.utc)


def _canon(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _hash(x):
    return sha256(_canon(x).encode("utf-8")).hexdigest()


def _text(value, field, maximum=150):
    if (type(value) is not str or not 1 <= len(value) <= maximum
            or value != value.strip() or _XML_BAD.search(value)
            or "\r" in value or "\n" in value or "\t" in value
            or value.lstrip().startswith(("=", "+", "-", "@"))
            or "javascript:" in value.lower()):
        _fail("office_document_" + field + "_unsafe", 422)
    return value


def _key(value):
    if type(value) is not str or _SAFE_KEY.fullmatch(value) is None:
        _fail("office_document_key_invalid", 422)
    return value


def _amount(value, *, money=False):
    if type(value) is not str or ( _MONEY if money else _NUM ).fullmatch(value) is None:
        _fail("office_document_decimal_invalid", 422)
    d = Decimal(value)
    if d > 999999999:
        _fail("office_document_decimal_overflow", 422)
    return d


def _normalize(body: DocumentCreate):
    if not body.lines or len(body.lines) > 60:
        _fail("office_document_lines_invalid", 422)
    seen=set()
    out=[]
    total=Decimal(0)
    with localcontext() as ctx:
        ctx.prec = 60
        for line in body.lines:
            line_ref=_key(line.line_ref)
            if line_ref in seen:
                _fail("office_document_duplicate_line", 422)
            seen.add(line_ref)
            label=_text(line.description,"description")
            unit=_text(line.unit,"unit",24)
            quantity=_amount(line.quantity)
            price=_amount(line.unit_price,money=True)
            if quantity <= 0:
                _fail("office_document_quantity_invalid",422)
            line_sum=(quantity*price).quantize(_TWO,rounding=ROUND_HALF_UP)
            total+=line_sum
            if total >= Decimal("1000000000000"):
                _fail("office_document_total_overflow",422)
            out.append({"line_ref":line_ref,"description":label,"unit":unit,
                "quantity":format(quantity,"f"),"unit_price":format(price,".2f"),
                "line_total":format(line_sum,".2f")})
    return {"title":_text(body.title,"title",120),"lines":out,
        "total_amount":format(total,".2f"),"currency":body.currency}


def _human(db,actor,permission):
    if actor.account_id is None or actor.session_id is None:
        _fail("office_document_human_session_required",403)
    db.flush()
    db.expire_all()
    for cap in (permission,"office.intake.read"):
        authorize_scope(db,actor,organization_id=actor.organization_id,
                        store_id=actor.store_id,permission=cap)
    if db.scalar(select(Subject.subject_type).where(
         Subject.organization_id==actor.organization_id,
         Subject.id==actor.subject_id))!="person":
        _fail("office_document_person_required",403)


def _lock(db,actor):
    # Same rows used by logout, account disable and personal grant changes;
    # caller reauthorizes after waiting and holds the transaction to commit.
    lock_employee_authority(db,actor)


def _source(db,actor,evidence_id,digest):
    if type(digest) is not str or not _SHA.fullmatch(digest):
        _fail("office_document_source_digest_invalid",422)
    try:
        item,content=evidence_bytes(db,actor,evidence_id)
    except OfficeIntakeError as exc:
        raise HTTPException(409,"office_document_source_unavailable_or_corrupt") from exc
    if item.sha256!=digest or sha256(content).hexdigest()!=digest:
        _fail("office_document_source_changed")
    return item


def _get(db,actor,identity):
    row=db.scalar(select(OfficeDocumentVersion).where(
        OfficeDocumentVersion.id==identity,
        OfficeDocumentVersion.organization_id==actor.organization_id,
        OfficeDocumentVersion.store_id==actor.store_id).execution_options(populate_existing=True))
    if row is None:
        _fail("office_document_not_found",404)
    return row


def _latest(db,actor,key):
    return db.scalar(select(OfficeDocumentVersion).where(
        OfficeDocumentVersion.organization_id==actor.organization_id,
        OfficeDocumentVersion.store_id==actor.store_id,
        OfficeDocumentVersion.document_key==key
    ).order_by(OfficeDocumentVersion.version.desc()).limit(1))


def _review(db,actor,row):
    return db.scalar(select(OfficeDocumentReview).where(
        OfficeDocumentReview.organization_id==actor.organization_id,
        OfficeDocumentReview.store_id==actor.store_id,
        OfficeDocumentReview.document_version_id==row.id))


def _export(db,actor,row,kind):
    return db.scalar(select(OfficeDocumentExport).where(
        OfficeDocumentExport.organization_id==actor.organization_id,
        OfficeDocumentExport.store_id==actor.store_id,
        OfficeDocumentExport.document_version_id==row.id,
        OfficeDocumentExport.file_format==kind))


def _validate(row):
    if (row.template_version not in (_TEMPLATE.get(row.document_type), _LEGACY_TEMPLATE.get(row.document_type))
          or row.content_sha256!=_hash(row.contents)
          or row.contents.get("currency")!=row.currency
          or row.contents.get("total_amount")!=row.total_amount
          or len(row.contents.get("lines",[]))>60):
        _fail("office_document_snapshot_tampered")
    with localcontext() as ctx:
        ctx.prec=60
        value=Decimal(0)
        for line in row.contents["lines"]:
            if (set(line)!={"line_ref","description","unit","quantity",
                            "unit_price","line_total"}):
                _fail("office_document_snapshot_tampered")
            quantity=_amount(line["quantity"])
            unit_price=_amount(line["unit_price"],money=True)
            actual=(quantity*unit_price).quantize(_TWO,rounding=ROUND_HALF_UP)
            if format(actual,".2f")!=line["line_total"]:
                _fail("office_document_snapshot_tampered")
            value+=actual
        if format(value,".2f")!=row.total_amount:
            _fail("office_document_snapshot_tampered")


def _view(db,actor,row,*,replayed=False):
    _validate(row)
    receipt=_review(db,actor,row)
    exports=list(db.scalars(select(OfficeDocumentExport).where(
        OfficeDocumentExport.organization_id==actor.organization_id,
        OfficeDocumentExport.store_id==actor.store_id,
        OfficeDocumentExport.document_version_id==row.id)))
    state=("draft" if receipt is None else "rejected" if receipt.decision=="rejected"
           else "exported" if exports else "approved")
    return {"id":row.id,"document_key":row.document_key,"version":row.version,
        "parent_id":row.parent_id,"document_type":row.document_type,
        "template_version":row.template_version,
        "evidence_id":row.evidence_id,"evidence_sha256":row.evidence_sha256,
        "content_sha256":row.content_sha256,"contents":row.contents,
        "total_amount":row.total_amount,"currency":row.currency,"state":state,
        "superseded":_latest(db,actor,row.document_key).id!=row.id,
        "submitted_by":row.submitted_by,"created_at":_utc(row.created_at).isoformat(),
        "review":None if receipt is None else {"decision":receipt.decision,
            "decided_by":receipt.decided_by,"reason":receipt.reason},
        "exports":[{"format":x.file_format,"sha256":x.content_sha256,
                    "byte_size":x.byte_size} for x in exports],
        "replayed":replayed,"formal_document":False,"printer_confirmed":False}


def create_document(db:Session,actor:AuthenticatedSubject,body:DocumentCreate,
                    request_key:str,*,now=None):
    _key(request_key)
    now=_utc(now) if now is not None else datetime.now(timezone.utc)
    _human(db,actor,"office.documents.write")
    normalized=_normalize(body)
    digest=_hash({"document_key":body.document_key,"expected_version":body.expected_version,
        "type":body.document_type,"evidence_id":body.evidence_id,
        "evidence_sha256":body.evidence_sha256,"contents":normalized})
    _lock(db,actor)
    _human(db,actor,"office.documents.write")
    old=db.scalar(select(OfficeDocumentVersion).where(
        OfficeDocumentVersion.organization_id==actor.organization_id,
        OfficeDocumentVersion.store_id==actor.store_id,
        OfficeDocumentVersion.request_key==request_key))
    if old is not None:
        if old.request_digest!=digest or old.submitted_by!=actor.subject_id:
            _fail("office_document_idempotency_conflict")
        _source(db,actor,old.evidence_id,old.evidence_sha256)
        return _view(db,actor,old,replayed=True)
    _source(db,actor,body.evidence_id,body.evidence_sha256)
    previous=_latest(db,actor,_key(body.document_key))
    if (previous.version if previous else 0)!=body.expected_version:
        _fail("office_document_version_conflict")
    if previous and previous.document_type!=body.document_type:
        _fail("office_document_type_change_denied")
    row=OfficeDocumentVersion(
        id=str(uuid4()),organization_id=actor.organization_id,
        store_id=actor.store_id,document_key=body.document_key,
        version=body.expected_version+1,parent_id=previous.id if previous else None,
        document_type=body.document_type,template_version=_TEMPLATE[body.document_type],
        evidence_id=body.evidence_id,evidence_sha256=body.evidence_sha256,
        submitted_by=actor.subject_id,request_key=request_key,
        request_digest=digest,content_sha256=_hash(normalized),
        contents=normalized,total_amount=normalized["total_amount"],
        currency=body.currency,created_at=now)
    db.add(row);db.flush()
    record_event(db,actor_id=actor.subject_id,organization_id=actor.organization_id,
        store_id=actor.store_id,action="office.document.version_created",
        target_type="office_document_version",target_id=row.id,
        idempotency_key="ofc005:create:"+row.id,
        details={"version":row.version,"content_sha256":row.content_sha256,
                 "evidence_sha256":row.evidence_sha256,"status":"draft"})
    return _view(db,actor,row)


def decide_document(db,actor,identity,body:DocumentDecision,key,*,now=None):
    _key(key)
    now=_utc(now) if now is not None else datetime.now(timezone.utc)
    _human(db,actor,"office.documents.review")
    _lock(db,actor)
    _human(db,actor,"office.documents.review")
    row=_get(db,actor,identity)
    _validate(row)
    if (body.expected_version!=row.version or body.content_sha256!=row.content_sha256):
        _fail("office_document_version_conflict")
    receipt=_review(db,actor,row)
    if receipt is not None:
        if (receipt.request_key!=key or receipt.decision!=body.decision
                or receipt.decided_by!=actor.subject_id or receipt.reason!=body.reason):
            _fail("office_document_review_consumed")
        return _view(db,actor,row,replayed=True)
    latest=_latest(db,actor,row.document_key)
    if latest.id!=row.id:
        _fail("office_document_superseded")
    if row.submitted_by==actor.subject_id:
        _fail("office_document_self_review_denied",403)
    _text(body.reason,"reason",1000)
    _source(db,actor,row.evidence_id,row.evidence_sha256)
    receipt=OfficeDocumentReview(
        id=str(uuid4()),organization_id=actor.organization_id,
        store_id=actor.store_id,document_version_id=row.id,
        decided_by=actor.subject_id,decision=body.decision,
        document_sha256=row.content_sha256,reason=body.reason,
        request_key=key,created_at=now)
    db.add(receipt);db.flush()
    record_event(db,actor_id=actor.subject_id,organization_id=actor.organization_id,
        store_id=actor.store_id,action="office.document.reviewed",
        target_type="office_document_version",target_id=row.id,
        idempotency_key="ofc005:review:"+receipt.id,
        details={"decision":body.decision,"content_sha256":row.content_sha256})
    return _view(db,actor,row)


def _rows(row):
    output=[["办公材料候选，不是正式业务单据",""],["类型",row.document_type],
        ["模板版本",row.template_version],["草稿编号",row.document_key],
        ["版本",str(row.version)],["材料SHA256",row.evidence_sha256],
        ["标题",row.contents["title"]],["币种",row.currency],
        ["行号","描述","单位","数量","单价","行金额"]]
    for line in row.contents["lines"]:
        output.append([line["line_ref"],line["description"],line["unit"],
                       line["quantity"],line["unit_price"],line["line_total"]])
    output.append(["总计","",row.currency,"","",row.total_amount])
    output.append(["此文件需人工核对，不代表已开票、已入库、已采购、已打印。"])
    return output


def _xml_cell(value,index,row_index):
    # Every user-controlled field is written as inlineStr, never Excel formula.
    text_value=str(value)
    if _XML_BAD.search(text_value):
        _fail("office_document_xlsx_character_invalid", 422)
    text_value=xml_escape(text_value, {'"':"&quot;", "'":"&apos;"})
    col=chr(65+index)
    return f'<c r="{col}{row_index}" t="inlineStr"><is><t xml:space="preserve">{text_value}</t></is></c>'


def _xlsx(row):
    lines=_rows(row)
    cells=[]
    for i,values in enumerate(lines,1):
        data="".join(_xml_cell(v,j,i) for j,v in enumerate(values))
        cells.append(f'<row r="{i}">{data}</row>')
    sheet=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           '<sheetViews><sheetView workbookViewId="0"/></sheetViews><sheetFormatPr defaultRowHeight="18"/>'
           '<sheetData>'+"".join(cells)+'</sheetData>'
           '<printOptions gridLines="false"/><pageSetup orientation="landscape" paperSize="9"/>'
           '</worksheet>').encode("utf-8")
    parts={
        "[Content_Types].xml":'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>',
        "_rels/.rels":'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        "xl/workbook.xml":'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="人工核对清单" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels":'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>',
        "xl/worksheets/sheet1.xml":sheet,
    }
    output=BytesIO()
    with ZipFile(output,"w",compression=ZIP_DEFLATED) as z:
        for name,payload in parts.items():
            info=ZipInfo(name,date_time=(1980,1,1,0,0,0))
            info.compress_type=ZIP_DEFLATED
            z.writestr(info,payload if isinstance(payload,bytes) else payload.encode("utf-8"))
    return output.getvalue()


def _pdf_lines(text, width=44):
    # Conservative 11pt full-em advance: even wide CJK glyphs fit A4 margins.
    # Never discard characters; supplementary glyphs are explicitly refused by
    # the PDF export instead of silently disappearing in a UCS2 font mapping.
    if any(ord(char) > 0xFFFF for char in text):
        _fail("office_document_pdf_character_unsupported", 422)
    return [text[i:i + width] for i in range(0, len(text), width)] or [""]


def _pdf(row):
    if row.template_version == _LEGACY_TEMPLATE.get(row.document_type):
        return _pdf_legacy(row)
    heading = ["人工核对预览 - 非正式单据", "编号 " + row.document_key + " / 版本 " + str(row.version)]
    heading = [line for text in heading for line in _pdf_lines(text)]
    blocks = [["标题：" + row.contents["title"],
              "模板：" + row.template_version + " / 币种：" + row.currency,
              "材料摘要：" + row.evidence_sha256]]
    for item in row.contents["lines"]:
        blocks.append(["行号：" + item["line_ref"], "商品：" + item["description"],
                       "单位：" + item["unit"] + " / 数量：" + item["quantity"],
                       "单价：" + item["unit_price"] + " / 行金额：" + item["line_total"]])
    blocks.append(["合计：" + row.currency + " " + row.total_amount,
                   "已由独立人工审核；不代表ERP落单、付款、打印或收货。"])
    capacity = 35 - len(heading) - 1
    chunks, current = [], []
    for block in blocks:
        lines = [line for text in block for line in _pdf_lines(text)]
        if len(lines) > capacity:
            _fail("office_document_pdf_layout_overflow")
        if current and len(current) + len(lines) > capacity:
            chunks.append(current)
            current = []
        current.extend(lines)
    if current:
        chunks.append(current)
    pages = [heading + chunk + [f"第 {index + 1} / {len(chunks)} 页"]
             for index, chunk in enumerate(chunks)]
    return _encode_pdf_pages(pages, line_height=19, modern=True)


def _pdf_legacy(row):
    # Built-in PDF CJK font; actual platform font/render quality is NOT validated.
    # Layout is intentionally bounded and always labelled as a draft copy.
    labels=["人工核对预览 - 非正式单据",row.contents["title"],
            row.document_type+" / "+row.template_version,
            "版本 "+str(row.version)+" / "+row.currency,
            "金额单位 "+row.currency+" / 合计 "+row.total_amount,
            "材料摘要 "+row.evidence_sha256[:24],
            "编号    描述    单位    数量    单价    行金额"]
    for line in row.contents["lines"]:
        labels.append(" | ".join([line["line_ref"][:10],line["description"][:19],
                     line["unit"][:8],line["quantity"],line["unit_price"],
                     line["line_total"]]))
    labels.append("已由独立人工审核；不代表ERP落单、付款、打印或收货")
    pages=[labels[k:k+24] for k in range(0,len(labels),24)]
    return _encode_pdf_pages(pages)


def _encode_pdf_pages(pages, line_height=29, modern=False):
    objects=[]
    def add(value):
        objects.append(value)
        return len(objects)
    root=add(b"")
    pages_id=add(b"")
    font=add(b"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light "
             b"/Encoding /UniGB-UCS2-H /DescendantFonts [5 0 R] >>")
    descendant=add(b"")
    cid=add(b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> >>")
    assert descendant==4 and cid==5
    if modern:
        # A referenced descriptor is required for readers to select their CJK
        # substitute font. Keep old receipt bytes untouched in legacy mode.
        objects[cid-1] = (b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
                         b"/FontDescriptor 4 0 R /DW 1000 "
                         b"/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> >>")
    page_ids=[]
    for page_num,lines in enumerate(pages,1):
        commands=["BT","/F1 11 Tf"]
        for i,txt in enumerate(lines):
            # CJK glyphs use two-byte Unicode input for UniGB-UCS2-H.
            safe=str(txt).encode("utf-16-be").hex().upper()
            commands.extend([f"1 0 0 1 44 {770-i*line_height} Tm",f"<{safe}> Tj"])
        commands.append("ET")
        stream="\n".join(commands).encode("ascii")
        stream_id=add(b"<< /Length "+str(len(stream)).encode()+b" >>\nstream\n"+
                      stream+b"\nendstream")
        pid=add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 {font} 0 R >> >> "
                f"/Contents {stream_id} 0 R >>".encode())
        page_ids.append(pid)
    objects[root-1]=f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
    objects[pages_id-1]=(
        f"<< /Type /Pages /Count {len(page_ids)} /Kids ["+
        " ".join(f"{pid} 0 R" for pid in page_ids)+"] >>").encode()
    objects[descendant-1]=b"<< /Type /FontDescriptor /FontName /STSong-Light /Flags 4 >>"
    if modern:
        objects[descendant-1] = (b"<< /Type /FontDescriptor /FontName /STSong-Light "
                                b"/Flags 6 /FontBBox [-25 -254 1000 880] "
                                b"/Ascent 752 /Descent -271 /CapHeight 737 "
                                b"/ItalicAngle 0 /StemV 58 >>")
    out=bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(out))
        out.extend(f"{i} 0 obj\n".encode()+obj+b"\nendobj\n")
    pos=len(out)
    out.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode())
    out.extend(f"trailer\n<< /Size {len(objects)+1} /Root {root} 0 R >>\n"
               f"startxref\n{pos}\n%%EOF\n".encode())
    return bytes(out)


def export_document(db,actor,identity,body:DocumentExport,key,*,now=None):
    _key(key)
    now=_utc(now) if now is not None else datetime.now(timezone.utc)
    _human(db,actor,"office.documents.export")
    _lock(db,actor)
    _human(db,actor,"office.documents.export")
    row=_get(db,actor,identity)
    _validate(row)
    if row.version!=body.expected_version or row.content_sha256!=body.content_sha256:
        _fail("office_document_version_conflict")
    if _latest(db,actor,row.document_key).id!=row.id:
        _fail("office_document_superseded")
    approval=_review(db,actor,row)
    if approval is None or approval.decision!="approved" or approval.document_sha256!=row.content_sha256:
        _fail("office_document_not_approved")
    _source(db,actor,row.evidence_id,row.evidence_sha256)
    prev=_export(db,actor,row,body.file_format)
    legacy_pdf = body.file_format == "pdf" and row.template_version == _LEGACY_TEMPLATE.get(row.document_type)
    if legacy_pdf and prev is None:
        _fail("office_document_pdf_requires_new_template_version")
    contents=_xlsx(row) if body.file_format=="xlsx" else _pdf(row)
    digest=sha256(contents).hexdigest()
    if prev:
        if (prev.request_key!=key or prev.exported_by!=actor.subject_id
              or prev.content_sha256!=digest or prev.byte_size!=len(contents)):
            _fail("office_document_export_conflict")
        replayed=True
    else:
        receipt=OfficeDocumentExport(
            id=str(uuid4()),organization_id=actor.organization_id,
            store_id=actor.store_id,document_version_id=row.id,
            exported_by=actor.subject_id,file_format=body.file_format,
            request_key=key,content_sha256=digest,byte_size=len(contents),
            created_at=now)
        db.add(receipt);db.flush()
        record_event(db,actor_id=actor.subject_id,
            organization_id=actor.organization_id,store_id=actor.store_id,
            action="office.document.exported",target_type="office_document_version",
            target_id=row.id,idempotency_key="ofc005:export:"+receipt.id,
            details={"file_format":body.file_format,"sha256":digest,"byte_size":len(contents),
                     "printer_confirmed":False,"formal_document":False})
        replayed=False
    return {"document":_view(db,actor,row,replayed=replayed),
            "file_format":body.file_format,"filename":row.document_key+"-v"+
                str(row.version)+"."+body.file_format,
            "content_sha256":digest,"content_base64":b64encode(contents).decode("ascii"),
            "byte_size":len(contents),"replayed":replayed,
            "warning": "历史PDF可能截短规格；请建立新版本重新审核导出。" if legacy_pdf else None,
            "printer_confirmed":False,"formal_document":False}


def read_export(db,actor,identity,file_format):
    _human(db,actor,"office.documents.export")
    lock_employee_authority(db,actor)
    _human(db,actor,"office.documents.export")
    row=_get(db,actor,identity)
    _validate(row)
    approval=_review(db,actor,row)
    if approval is None or approval.decision!="approved" or approval.document_sha256!=row.content_sha256:
        _fail("office_document_not_approved")
    _source(db,actor,row.evidence_id,row.evidence_sha256)
    receipt=_export(db,actor,row,file_format)
    if receipt is None:
        _fail("office_document_export_not_found",404)
    contents=_xlsx(row) if file_format=="xlsx" else _pdf(row)
    digest=sha256(contents).hexdigest()
    if receipt.content_sha256!=digest or receipt.byte_size!=len(contents):
        _fail("office_document_export_integrity_error")
    legacy_pdf=file_format=="pdf" and row.template_version==_LEGACY_TEMPLATE.get(row.document_type)
    return {"document":_view(db,actor,row),"file_format":file_format,
            "filename":row.document_key+"-v"+str(row.version)+"."+file_format,
            "content_sha256":digest,"content_base64":b64encode(contents).decode("ascii"),
            "byte_size":len(contents),"replayed":True,
            "warning":"历史PDF可能截短规格；请建立新版本重新审核导出。" if legacy_pdf else None,
            "printer_confirmed":False,"formal_document":False}


def command_status(db,actor,action,key):
    permission={"create":"office.documents.write","review":"office.documents.review",
                "export":"office.documents.export"}[action]
    _human(db,actor,permission)
    lock_employee_authority(db,actor)
    _human(db,actor,permission)
    table,who={"create":(OfficeDocumentVersion,OfficeDocumentVersion.submitted_by),
               "review":(OfficeDocumentReview,OfficeDocumentReview.decided_by),
               "export":(OfficeDocumentExport,OfficeDocumentExport.exported_by)}[action]
    found=db.scalar(select(table.id).where(table.organization_id==actor.organization_id,
        table.store_id==actor.store_id,table.request_key==key,who==actor.subject_id))
    return {"status":"committed" if found else "unknown"}


def read_document(db,actor,identity):
    _human(db,actor,"office.documents.read")
    _lock(db,actor)
    _human(db,actor,"office.documents.read")
    return _view(db,actor,_get(db,actor,identity))


def list_documents(db,actor):
    _human(db,actor,"office.documents.read")
    _lock(db,actor)
    _human(db,actor,"office.documents.read")
    rows=db.scalars(select(OfficeDocumentVersion).where(
        OfficeDocumentVersion.organization_id==actor.organization_id,
        OfficeDocumentVersion.store_id==actor.store_id).order_by(
        OfficeDocumentVersion.created_at.desc(),OfficeDocumentVersion.id.desc()).limit(40))
    return [_view(db,actor,r) for r in rows]


def _envelope(data):
    return DocumentEnvelope(success=True,data=data,code="OK",error=None,
        user_message="仅为内部文档草稿及审核导出；不是管家婆正式单据或打印回执。",hint=None)


@router.get("",response_model=DocumentEnvelope)
def document_list(actor:AuthenticatedSubject=Depends(require_authenticated_subject),
                  db:Session=Depends(get_session)):
    return _envelope({"items":list_documents(db,actor)})


@router.post("",response_model=DocumentEnvelope,status_code=201)
def document_create(body:DocumentCreate,
    actor:AuthenticatedSubject=Depends(require_authenticated_subject),
    db:Session=Depends(get_session),
    key:str=Depends(require_idempotency_key)):
    try:
        result=create_document(db,actor,body,key);db.commit()
        return _envelope(result)
    except IntegrityError as exc:
        db.rollback();raise HTTPException(409,"office_document_version_conflict") from exc
    except Exception:
        db.rollback();raise


@router.get("/commands/status",response_model=DocumentEnvelope)
def document_command_status(action:Literal["create","review","export"],
    actor:AuthenticatedSubject=Depends(require_authenticated_subject),
    db:Session=Depends(get_session),key:str=Depends(require_idempotency_key)):
    return _envelope(command_status(db,actor,action,key))


@router.get("/{identity}/exports/{file_format}",response_model=DocumentEnvelope)
def document_read_export(identity:str,file_format:Literal["pdf","xlsx"],
    actor:AuthenticatedSubject=Depends(require_authenticated_subject),db:Session=Depends(get_session)):
    return _envelope(read_export(db,actor,identity,file_format))


@router.get("/{identity}",response_model=DocumentEnvelope)
def document_get(identity:str,actor:AuthenticatedSubject=Depends(require_authenticated_subject),
                 db:Session=Depends(get_session)):
    return _envelope(read_document(db,actor,identity))


@router.post("/{identity}/review",response_model=DocumentEnvelope,status_code=201)
def document_review(identity:str,body:DocumentDecision,
    actor:AuthenticatedSubject=Depends(require_authenticated_subject),
    db:Session=Depends(get_session),
    key:str=Depends(require_idempotency_key)):
    try:
        result=decide_document(db,actor,identity,body,key);db.commit()
        return _envelope(result)
    except IntegrityError as exc:
        db.rollback();raise HTTPException(409,"office_document_review_consumed") from exc
    except Exception:
        db.rollback();raise


@router.post("/{identity}/export",response_model=DocumentEnvelope,status_code=201)
def document_export(identity:str,body:DocumentExport,
    actor:AuthenticatedSubject=Depends(require_authenticated_subject),
    db:Session=Depends(get_session),
    key:str=Depends(require_idempotency_key)):
    try:
        result=export_document(db,actor,identity,body,key);db.commit()
        return _envelope(result)
    except IntegrityError as exc:
        db.rollback();raise HTTPException(409,"office_document_export_conflict") from exc
    except Exception:
        db.rollback();raise
