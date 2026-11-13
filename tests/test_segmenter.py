"""主单识别与页面分段规则。"""

from app.collector.segmenter import (
    PageKind,
    classify_page_by_rules,
    classify_pages,
    detect_master_bill,
    find_hawb_no,
    group_segments,
)
from tests.conftest import MANIFEST_PAGE, MASTER_PAGE, house_page


def test_master_bill_detected():
    signal = detect_master_bill(MASTER_PAGE)
    assert signal.is_master is True
    assert signal.master_no == "999-12345678"
    assert any("MAWB" in item.upper() or "MASTER" in item.upper() for item in signal.evidence)


def test_house_bill_with_mawb_reference_is_not_master():
    text = house_page("HAWB0001")
    signal = detect_master_bill(text)
    assert signal.is_master is False
    kind = classify_page_by_rules(text, previous_kind=None)
    assert kind.kind == "house_bill"
    assert kind.doc_no == "HAWB0001"


def test_manifest_classified_even_with_hawb_column():
    kind = classify_page_by_rules(MANIFEST_PAGE, previous_kind=None)
    assert kind.kind == "manifest"


def test_house_bill_mentioning_manifest_in_footer_is_still_house_bill():
    text = house_page("HAWB0001") + "\nNOT PART OF MANIFEST\n"
    kind = classify_page_by_rules(text, previous_kind=None)
    assert kind.kind == "house_bill"


# ---- 真实单证版式（样例 416-52331591主分仓.pdf） ----

REAL_MASTER_PAGE = """416 | PVG 52331591
416- 52331591
Shipper's Name and Address
8&9F, RAFFLES CITY CHANGNING OFFI
TOWER 3, NO. 1193 CHANGNING RD
SHANGHAI 200051 CHINA
TEL: +86 21 33259800 FAX: +86 21 33259923
EXPAIR.PVG@CN.DSV.COM
Shipper's Account Number
Not Negotiable
Air Waybill
Issued by
NATIONAL AIRLINES
Consignee's Name and Address
DALLAS/FORTH WORTH INTERNATIONAL AIRPORT
Airport of Departure (Addr. of First Carrier) and requested Routing
PVG
Handling Information
CONSOLIDATION AS PER ATTACHED MANIFEST
416- 52331591
"""

REAL_HOUSE_PAGE = """416PVG 52331591
HAWB No: EDC2037060
Shipper's Name and Address
KUNSHAN KING SON TECH CO.,LTD.
Issued by
DSV AIR & SEA CO., LTD.
Consignee's Name and Address
KEMET ELECTRONICS CORPORATION
1705 BILLY MITCHELL BLVD
Issuing Carrier's Agent Name and City
Airport of Departure (Addr. of First Carrier) and Requested Routing
SHANGHAI PUDONG INTERNATIONAL APT
Airport of Destination
DALLAS-FORT WORTH INTN8652/30 SEP/26
Pieces 1 440.00 K
"""

REAL_MANIFEST_PAGE = """CONSOL
CCN4017377
MAWB
41652331591
DATE
28-Sep-26 15:17
Air Freight Manifest
Page 1 of 2
CONSOL DETAILS
TOTAL WEIGHT
TOTAL VOLUME
CHARGEABLE
SHIPMENT DETAILS
HOUSE
SHIPPER
CONSIGNEE
HAWB:
EDC2037060
Wgt / Vol / Pkg: 440.00 KG / 2.450 M3 / 1 CTN
Origin:
CNPVG
Destination:
USDFW
"""

MASTER_ONLY_TEXT = """KALITTA AIR
MAILING ADDRESS:
818 WILLOW RUN AIRPORT
272-76217971
272-76217971
272-76217971
CONSOLIDATION AS PER
ATTACHED MANIFEST
VOL:11.007 CBM
"""


def test_real_scanned_master_page_is_master_not_manifest():
    assert detect_master_bill(REAL_MASTER_PAGE).is_master is True
    kind = classify_page_by_rules(REAL_MASTER_PAGE, previous_kind=None)
    assert kind.kind == "master_bill"
    assert kind.doc_no == "416-52331591"


def test_real_manifest_page_is_manifest_even_with_hawb_column():
    kind = classify_page_by_rules(REAL_MANIFEST_PAGE, previous_kind=None)
    assert kind.kind == "manifest"


def test_real_house_page_is_house_with_doc_no():
    kind = classify_page_by_rules(REAL_HOUSE_PAGE, previous_kind=None)
    assert kind.kind == "house_bill"
    assert kind.doc_no == "EDC2037060"


def test_master_only_page_without_master_label_is_still_master():
    signal = detect_master_bill(MASTER_ONLY_TEXT)
    assert signal.is_master is True
    assert signal.master_no == "272-76217971"
    assert classify_page_by_rules(MASTER_ONLY_TEXT, previous_kind=None).kind == "master_bill"


def test_hawb_number_not_confused_with_title():
    assert find_hawb_no("HOUSE AIR WAYBILL\nHAWB NO.: SHA1234567\n") == "SHA1234567"


def test_air_waybill_title_without_hawb_is_master():
    text = "Air Waybill\nNot Negotiable\n272-76217971\nPVG\nDFW\n"
    signal = detect_master_bill(text)
    assert signal.is_master is True
    assert signal.master_no == "272-76217971"


def test_air_waybill_title_with_hawb_is_house():
    text = "Air Waybill\nHAWB NO.: EDC2037060\n272-76217971\n"
    assert detect_master_bill(text).is_master is False


def test_continuation_page_merges_into_previous_house_bill():
    pages = [
        (1, house_page("HAWB0001")),
        (2, "Continued from previous page\nShipper: SHANGHAI EXAMPLE TRADING CO., LTD\n"
            "Destination: LAX\nFlight: CA1234\nMarks: N/M\n"),
        (3, MANIFEST_PAGE),
    ]
    kinds = classify_pages(pages)
    assert kinds[1].kind == "house_bill"
    assert kinds[1].continuation is True

    segments = group_segments(kinds)
    house_segments = [item for item in segments if item.kind == "house_bill"]
    manifest_segments = [item for item in segments if item.kind == "manifest"]
    assert len(house_segments) == 1
    assert house_segments[0].page_indexes == [1, 2]
    assert len(manifest_segments) == 1


def test_two_house_bills_are_split_by_header():
    kinds = [
        PageKind(index=1, kind="master_bill", confidence=0.9),
        PageKind(index=2, kind="house_bill", doc_no="HAWB0001", confidence=0.9),
        PageKind(index=3, kind="house_bill", doc_no="HAWB0002", confidence=0.9),
        PageKind(index=4, kind="manifest", confidence=0.9),
        PageKind(index=5, kind="manifest", continuation=True, confidence=0.6),
    ]
    segments = group_segments(kinds)
    assert [item.kind for item in segments] == [
        "master_bill",
        "house_bill",
        "house_bill",
        "manifest",
    ]
    assert segments[3].page_indexes == [4, 5]


NOTICE_PAGE = """NOTICE CONCERNING CARRIER'S LIMITATION OF LIABILITY
If the carriage involves an ultimate destination or stop in a country other
than the country of departure, the Warsaw Convention may be applicable.
Air Waybill
Shipper's Name and Address
Consignee's Name and Address
Airport of Departure and Requested Routing
"""


def test_notice_page_without_master_no_is_not_master_bill():
    """条款页含 Air Waybill、航班/当事人等词，但没有主单号，不能判主单。"""

    signal = detect_master_bill(NOTICE_PAGE)
    assert signal.is_master is False
    kind = classify_page_by_rules(NOTICE_PAGE, previous_kind=None)
    assert kind.kind != "master_bill"


HOUSE_AWB_PAGE = """Air Waybill
416 PVG 52342905 SZA26100005
Shipper's Name and Address
XDK COMMUNICATION EQUIPMENT (HUIZHOU) LTD
Consignee's Name and Address
AFL TELECOMMUNICATIONS LLC
Issued by TCI INTERNATIONAL LOGISTICS (SHANGHAI) LIMITED
Requested Flight/Date
"""


def test_house_awb_with_forwarder_number_is_house_bill():
    """页首同时印主单号与货代运单号的 AWB，要判分单而不是主单。

    这种版式没有 "HAWB" 标签，以前会命中 "Air Waybill" + 主单号被判成主单，
    单票模式下直接触发「多个主单」导致整单失败。
    """

    kind = classify_page_by_rules(HOUSE_AWB_PAGE, previous_kind=None)

    assert kind.kind == "house_bill"
    assert kind.doc_no == "SZA26100005"
    assert detect_master_bill(HOUSE_AWB_PAGE).is_master is False


def test_manifest_with_hawb_column_is_not_house_bill():
    """舱单表头也有 HAWB 列，不能被「页首有货代单号」抢判成分单。"""

    text = """CARGO MANIFEST
HAWB NO. | ORIGIN | DEST | PCS | G.W.
WPL26100101 | PVG | DFW | 45 | 6687
Air Waybill
"""
    kind = classify_page_by_rules(text, previous_kind=None)

    assert kind.kind == "manifest"
