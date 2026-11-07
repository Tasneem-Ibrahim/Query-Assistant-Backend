"""Generate the two sample PDFs used by the project.

    python scripts/make_sample_pdfs.py

Creates:
    pdfs/customer_support/NovaMart_Customer_Support_Handbook.pdf
    pdfs/legal_assistant/Master_Services_Agreement.pdf

Some pages contain images that carry TEXT (tables / infographics). Those are the
"Text in Image" part of the diagram and are read by OCR during ingestion.
Both documents are fictional and exist only to demo the RAG pipeline.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Image as RLImage,
    KeepTogether,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parent.parent
CS_PDF = ROOT / "pdfs" / "customer_support" / "NovaMart_Customer_Support_Handbook.pdf"
LEGAL_PDF = ROOT / "pdfs" / "legal_assistant" / "Master_Services_Agreement.pdf"

# --------------------------------------------------------------------------- #
# Fonts / images (images are drawn with high contrast so OCR works well)
# --------------------------------------------------------------------------- #
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in _BOLD_CANDIDATES if bold else _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def table_image(title: str, headers: list[str], rows: list[list[str]], widths: list[int]) -> io.BytesIO:
    """Draw a simple table as a PNG (text inside an image)."""
    pad, row_h, title_h = 14, 54, 70
    width = sum(widths) + 2 * pad
    height = title_h + row_h * (len(rows) + 1) + 2 * pad
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    d.text((pad, pad + 8), title, font=_font(28, True), fill="black")
    y = title_h + pad
    # header
    x = pad
    d.rectangle([pad, y, width - pad, y + row_h], fill=(30, 60, 114))
    for h, w in zip(headers, widths):
        d.text((x + 10, y + 14), h, font=_font(22, True), fill="white")
        x += w
    y += row_h
    for i, row in enumerate(rows):
        x = pad
        d.rectangle([pad, y, width - pad, y + row_h], fill=(238, 242, 250) if i % 2 == 0 else "white")
        for cell, w in zip(row, widths):
            d.text((x + 10, y + 14), cell, font=_font(21), fill="black")
            x += w
        y += row_h
    d.rectangle([pad, title_h + pad, width - pad, y], outline="black", width=2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


def steps_image(title: str, steps: list[str]) -> io.BytesIO:
    """Draw numbered boxes (an infographic) as a PNG."""
    pad, box_h, gap, title_h = 16, 70, 18, 70
    width = 1100
    height = title_h + len(steps) * (box_h + gap) + pad
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    d.text((pad, pad + 6), title, font=_font(28, True), fill="black")
    y = title_h
    for n, step in enumerate(steps, start=1):
        d.rounded_rectangle([pad, y, width - pad, y + box_h], radius=12, fill=(232, 244, 236), outline="black", width=2)
        d.ellipse([pad + 14, y + 13, pad + 58, y + 57], fill=(30, 110, 60))
        d.text((pad + 29, y + 20), str(n), font=_font(24, True), fill="white", anchor="mt")
        d.text((pad + 80, y + 22), step, font=_font(24), fill="black")
        y += box_h + gap
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


def rl_image(buf: io.BytesIO, width_cm: float = 16.5) -> RLImage:
    with Image.open(buf) as im:
        w, h = im.size
    buf.seek(0)
    width = width_cm * cm
    return RLImage(buf, width=width, height=width * h / w)


# --------------------------------------------------------------------------- #
# Shared reportlab helpers
# --------------------------------------------------------------------------- #
_ss = getSampleStyleSheet()
TITLE = ParagraphStyle("T", parent=_ss["Title"], fontSize=24, leading=30, spaceAfter=10)
SUB = ParagraphStyle("S", parent=_ss["Normal"], fontSize=12, leading=16, alignment=1, textColor=colors.HexColor("#444444"))
H1 = ParagraphStyle("H1", parent=_ss["Heading1"], fontSize=16, leading=20, spaceBefore=6, spaceAfter=8, textColor=colors.HexColor("#1e3c72"))
H2 = ParagraphStyle("H2", parent=_ss["Heading2"], fontSize=12.5, leading=16, spaceBefore=8, spaceAfter=4)
BODY = ParagraphStyle("B", parent=_ss["Normal"], fontSize=10.5, leading=15, alignment=TA_JUSTIFY, spaceAfter=6)
SMALL = ParagraphStyle("Sm", parent=BODY, fontSize=9, leading=12, textColor=colors.HexColor("#555555"))
CAPTION = ParagraphStyle("C", parent=SMALL, alignment=1, spaceBefore=3, spaceAfter=10)


class NumberedCanvas(canvas.Canvas):
    """Canvas that can print 'Page X of Y' and a running header."""

    header_text = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self.setFont("Helvetica", 8.5)
            self.setFillColor(colors.HexColor("#666666"))
            self.drawString(2 * cm, A4[1] - 1.3 * cm, self.header_text)
            self.line(2 * cm, A4[1] - 1.5 * cm, A4[0] - 2 * cm, A4[1] - 1.5 * cm)
            self.drawCentredString(A4[0] / 2, 1.1 * cm, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


def canvas_with_header(text: str):
    class _C(NumberedCanvas):
        header_text = text

    return _C


def P(text: str, style=BODY) -> Paragraph:
    return Paragraph(text, style)


def bullets(items: list[str]) -> ListFlowable:
    return ListFlowable(
        [ListItem(Paragraph(i, BODY), leftIndent=14) for i in items],
        bulletType="bullet",
        start="-",
        leftIndent=14,
    )


def numbered(items: list[str]) -> ListFlowable:
    return ListFlowable(
        [ListItem(Paragraph(i, BODY), leftIndent=16) for i in items],
        bulletType="1",
        leftIndent=16,
    )


def grid(data: list[list[str]], widths: list[float], header: bool = True) -> Table:
    rows = [[Paragraph(str(c), ParagraphStyle("c", parent=BODY, alignment=0, fontSize=9.5, leading=12.5, spaceAfter=0)) for c in r] for r in data]
    t = Table(rows, colWidths=[w * cm for w in widths], repeatRows=1 if header else 0)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dde6f5")))
    t.setStyle(TableStyle(style))
    return t


# =========================================================================== #
# 1) CUSTOMER SUPPORT HANDBOOK  (fictional online store: NovaMart)
# =========================================================================== #
def build_customer_support() -> None:
    s: list = []

    # ---- Page 1: cover + overview + channels
    s += [
        Spacer(1, 1.2 * cm),
        P("NovaMart Customer Support Handbook", TITLE),
        P("Customer help guide for orders, delivery, returns, warranty and account support", SUB),
        P("Version 3.2 - Effective 1 January 2026", SUB),
        Spacer(1, 0.8 * cm),
        P("1. About NovaMart", H1),
        P(
            "NovaMart is an online store that sells electronics, home appliances, fashion, grocery items and "
            "everyday essentials through its website and mobile app. This handbook explains how NovaMart "
            "customer support works and what customers can expect for orders, payments, shipping, returns, "
            "refunds, warranty and account security. It is written for customers and for support agents so "
            "that everyone gives and receives the same answers."
        ),
        P("2. How to contact support", H1),
        grid(
            [
                ["Channel", "Details", "Availability"],
                ["Live chat (app and website)", "Fastest way to get help; chat history is saved to your account", "24 hours a day, 7 days a week"],
                ["Phone helpline", "0800-555-0199 (toll free)", "Monday to Saturday 9:00 AM to 9:00 PM; Sunday 11:00 AM to 6:00 PM"],
                ["Email", "support@novamart.example", "Reply within 24 hours"],
                ["Help Center", "help.novamart.example with articles and order tracking", "Always available"],
            ],
            [4.3, 7.0, 5.7],
        ),
        Spacer(1, 0.3 * cm),
        P(
            "Every request receives a ticket number in the format NM-123456. Please keep the ticket number and "
            "quote it whenever you contact support again about the same issue.",
        ),
        PageBreak(),
    ]

    # ---- Page 2: account & security
    s += [
        P("3. Account and security", H1),
        P("3.1 Creating an account", H2),
        P(
            "You can register with an email address or a mobile number. A one-time verification code (OTP) is "
            "sent to confirm your details. The code is valid for 10 minutes."
        ),
        P("3.2 Forgot password", H2),
        numbered(
            [
                "On the login screen tap <b>Forgot Password</b>.",
                "Enter your registered email or mobile number.",
                "Open the reset link or enter the OTP you receive. The reset link is valid for 15 minutes.",
                "Choose a new password with at least 8 characters, including one number and one capital letter.",
            ]
        ),
        P("3.3 Locked account", H2),
        P(
            "After 5 wrong password attempts your account is locked for 30 minutes for your protection. "
            "You can wait for the lock to end or reset your password to unlock it immediately."
        ),
        P("3.4 Two-factor authentication (2FA)", H2),
        P(
            "2FA is optional but recommended. Turn it on under Settings &gt; Security. NovaMart staff will "
            "never ask you for your password or OTP, by phone, chat or email. If someone asks, do not share "
            "it and report it to support immediately."
        ),
        P("3.5 Deleting your account", H2),
        P(
            "Open Settings &gt; Privacy &gt; Delete Account to send a request. Deletion is completed within "
            "14 days. Pending orders, refunds and unused NovaPoints must be settled first; deleted accounts "
            "cannot be recovered and unused points are lost."
        ),
        PageBreak(),
    ]

    # ---- Page 3: orders & payments
    s += [
        P("4. Orders and payments", H1),
        P("4.1 Payment methods", H2),
        bullets(
            [
                "Credit and debit cards (Visa and Mastercard)",
                "Mobile wallets and bank transfer",
                "Cash on Delivery (COD) for orders up to $200",
                "Gift cards and NovaPoints (can be combined with any other method)",
            ]
        ),
        P("4.2 Changing or cancelling an order", H2),
        P(
            "You can edit the delivery address or items free of charge within <b>1 hour</b> of placing the "
            "order. You can cancel an order any time before its status changes to <b>Shipped</b>. Once an "
            "order is shipped it cannot be cancelled; you can refuse the parcel at the door or return it "
            "after delivery under the Returns Policy."
        ),
        P("4.3 Order statuses", H2),
        grid(
            [
                ["Status", "Meaning"],
                ["Confirmed", "Payment received or COD accepted; order is being prepared"],
                ["Packed", "Items are packed and waiting for the courier"],
                ["Shipped", "Handed to the courier; a tracking number is available"],
                ["Out for delivery", "The courier will deliver today"],
                ["Delivered", "Parcel was handed over to you or a person at your address"],
            ],
            [4.5, 12.5],
        ),
        Spacer(1, 0.2 * cm),
        P("4.4 Payment failed but money was deducted", H2),
        P(
            "This sometimes happens when the bank connection drops. In most cases the amount is automatically "
            "reversed to your account within 3 to 5 business days. If it is not reversed after 5 business "
            "days, contact support with your order number, the transaction ID and a screenshot of the "
            "deduction message."
        ),
        PageBreak(),
    ]

    # ---- Page 4: shipping & delivery (+ image with text, partly duplicate, partly unique)
    ship_img = table_image(
        "Delivery options at a glance",
        ["Option", "Delivery time", "Charge", "Order cut-off time"],
        [
            ["Standard", "3-5 business days", "$4.99", "11:59 PM"],
            ["Express", "1-2 business days", "$9.99", "3:00 PM"],
            ["Same-day", "Within 8 hours", "$14.99", "12:00 PM"],
            ["International", "7-14 business days", "$24.99", "11:59 PM"],
        ],
        [220, 280, 160, 330],
    )
    s += [
        P("5. Shipping and delivery", H1),
        P(
            "NovaMart delivers with trusted courier partners. Delivery times start after the order is "
            "<b>Shipped</b>, not from the moment you place it. Business days are Monday to Saturday and "
            "exclude public holidays."
        ),
        bullets(
            [
                "<b>Standard delivery:</b> 3 to 5 business days, $4.99. Free on orders above $50.",
                "<b>Express delivery:</b> 1 to 2 business days, $9.99.",
                "<b>Same-day delivery:</b> available in selected cities only, $14.99.",
                "<b>International delivery:</b> 7 to 14 business days, flat $24.99.",
            ]
        ),
        P(
            "Failed delivery: the courier makes up to 3 delivery attempts. After the third failed attempt the "
            "parcel returns to the NovaMart warehouse and a refund is processed, minus the shipping fee for "
            "prepaid orders. You can change the delivery address only before the order is Shipped."
        ),
        Spacer(1, 0.2 * cm),
        KeepTogether([rl_image(ship_img), P("Figure 1 - Delivery options and order cut-off times", CAPTION)]),
        PageBreak(),
    ]

    # ---- Page 5-6: returns & refunds (steps image duplicates the text; timeline image is unique)
    steps = steps_image(
        "How to return an item in 4 steps",
        [
            "Open My Orders and select Return Item",
            "Choose the reason and upload photos if damaged",
            "Courier collects the parcel from your address",
            "Refund is issued after inspection",
        ],
    )
    refund_img = table_image(
        "Refund timelines after inspection",
        ["Original payment method", "Time to receive refund"],
        [
            ["Credit / debit card", "5-7 business days"],
            ["Mobile wallet", "1-2 business days"],
            ["Bank transfer", "7-10 business days"],
            ["Cash on Delivery", "7-10 business days (bank transfer or store credit)"],
            ["NovaPoints / gift card", "Instant"],
        ],
        [420, 640],
    )
    s += [
        P("6. Returns and refunds", H1),
        P("6.1 Return window", H2),
        P(
            "Most items can be returned within <b>14 days</b> of delivery. Electronics and mobile phones have "
            "a shorter window of <b>7 days</b>. Items must be unused, in original packaging with all tags, "
            "accessories and the invoice."
        ),
        P("6.2 How to return an item", H2),
        numbered(
            [
                "Open My Orders and select <b>Return Item</b>.",
                "Choose the reason and upload photos if the item is damaged.",
                "A courier collects the parcel from your address.",
                "The refund is issued after the item passes inspection.",
            ]
        ),
        KeepTogether([rl_image(steps, 15), P("Figure 2 - Return process", CAPTION)]),
        P("6.3 Who pays for return shipping?", H2),
        bullets(
            [
                "<b>Damaged, defective or wrong item:</b> return pickup is free. Report it within <b>48 hours</b> of "
                "delivery with photos or a short video of the parcel and item.",
                "<b>Change of mind:</b> a return shipping fee of $3.99 is deducted from the refund.",
            ]
        ),
        PageBreak(),
        P("6.4 Items that cannot be returned", H2),
        bullets(
            [
                "Perishable food and grocery items",
                "Opened underwear, swimwear and personal care items",
                "Gift cards and digital downloads",
                "Customized or personalized products",
                "Clearance items marked <i>Final Sale</i>",
            ]
        ),
        P("6.5 Refunds", H2),
        P(
            "Refunds go back to the original payment method. The refund process starts 1 to 2 business days "
            "after the returned item reaches our warehouse and passes inspection. The time it then takes to "
            "appear in your account depends on how you paid, as shown below. For Cash on Delivery orders you "
            "choose between bank transfer and store credit."
        ),
        KeepTogether([rl_image(refund_img, 15), P("Figure 3 - Refund timelines by payment method", CAPTION)]),
        P(
            "If an item fails inspection (for example it is used or damaged by the customer) it is sent back "
            "to you and no refund is made.",
        ),
        PageBreak(),
    ]

    # ---- Page 7: warranty
    s += [
        P("7. Warranty and repairs", H1),
        grid(
            [
                ["Product type", "Warranty period"],
                ["Electronics and home appliances", "12 months manufacturer warranty"],
                ["Accessories (cables, chargers, cases)", "6 months"],
                ["Fashion and grocery", "No warranty; see Returns Policy"],
            ],
            [8.5, 8.5],
        ),
        Spacer(1, 0.3 * cm),
        P("7.1 What you need", H2),
        P("Keep your NovaMart invoice. The warranty starts on the delivery date and a valid invoice is required for every claim."),
        P("7.2 What is not covered", H2),
        bullets(
            [
                "Physical damage such as drops, cracks and bent parts",
                "Water or liquid damage",
                "Repairs or opening of the product by an unauthorised person",
                "Normal wear and tear and cosmetic scratches",
            ]
        ),
        P("7.3 Repair process", H2),
        P(
            "Create a warranty request in My Orders &gt; Request Warranty. A repair normally takes 7 to 10 "
            "business days. If the product cannot be repaired, NovaMart replaces it with the same or an "
            "equivalent model. A replacement does not restart the warranty; the remaining period continues."
        ),
        PageBreak(),
    ]

    # ---- Page 8: troubleshooting
    s += [
        P("8. Troubleshooting guide", H1),
        grid(
            [
                ["Problem", "What to do"],
                ["App crashes or does not open", "Update the app to the latest version, restart the phone, clear the app cache. If it continues, uninstall and reinstall the app."],
                ["Order tracking not updating", "Tracking can take up to 24 hours to refresh. If there is no update for 48 hours, contact support with your order number."],
                ["Coupon code not working", "Check the expiry date and the minimum order value. Only one coupon can be used per order and coupons cannot be combined with Clearance items."],
                ["Did not receive OTP", "Wait 60 seconds and request again. Check the number is correct and that SMS or email is not blocked. You can request at most 5 OTPs per hour."],
                ["Wrong item delivered", "Report within 48 hours with photos. A free return pickup and replacement or refund will be arranged."],
                ["Parcel marked Delivered but not received", "Check with neighbours or the building reception, then contact support within 24 hours so we can investigate with the courier."],
            ],
            [5.2, 11.8],
        ),
        PageBreak(),
    ]

    # ---- Page 9: escalation (image with unique info)
    sla_img = table_image(
        "Escalation matrix and response times",
        ["Level", "Handled by", "First response time"],
        [
            ["Level 1", "Support Agent", "Chat: 15 minutes / Email: 24 hours"],
            ["Level 2", "Senior Agent", "4 hours"],
            ["Level 3", "Team Lead", "24 hours"],
            ["Level 4", "Customer Care Manager", "48 hours"],
        ],
        [200, 380, 520],
    )
    s += [
        P("9. Complaints and escalation", H1),
        P(
            "If your issue is not solved by the first agent, you can ask to escalate it. Escalation moves the "
            "ticket upward through four levels: Level 1 Support Agent, Level 2 Senior Agent, Level 3 Team "
            "Lead and Level 4 Customer Care Manager. Always keep your ticket number ready. Each level has its "
            "own first response time, shown in the figure."
        ),
        KeepTogether([rl_image(sla_img, 16), P("Figure 4 - Escalation matrix and response times", CAPTION)]),
        P("When to escalate", H2),
        bullets(
            [
                "No reply within the response time of your current level",
                "A refund is delayed beyond the timeline for your payment method",
                "You received incorrect or conflicting information from support",
                "Your issue involves account security or suspected fraud (escalate immediately)",
            ]
        ),
        PageBreak(),
    ]

    # ---- Page 10: loyalty & coupons
    s += [
        P("10. NovaPoints and coupons", H1),
        P(
            "Customers earn <b>1 NovaPoint for every $1</b> spent on eligible items. <b>100 NovaPoints equal $1</b> "
            "of discount at checkout. Points expire <b>12 months</b> after they are earned. Points are not earned "
            "on shipping fees, gift cards or returned items; points earned from a returned order are removed."
        ),
        grid(
            [
                ["Tier", "Yearly spend", "Benefits"],
                ["Silver", "$0 - $499", "Standard earning rate"],
                ["Gold", "$500 - $1,499", "1.25x points and early access to sales"],
                ["Platinum", "$1,500 and above", "1.5x points, free express delivery on orders above $30"],
            ],
            [3.5, 4.5, 9.0],
        ),
        Spacer(1, 0.3 * cm),
        P("Coupon rules", H2),
        bullets(
            [
                "One coupon per order; coupons cannot be combined with each other.",
                "Coupons are not valid on Clearance items or gift cards.",
                "A coupon used on an order that is later cancelled is returned to your account if it has not expired.",
            ]
        ),
        PageBreak(),
    ]

    # ---- Page 11: Roman Urdu quick help
    s += [
        P("11. Quick Help in Roman Urdu (Asaan madad)", H1),
        P(
            "Ye hissa un customers ke liye hai jo Roman Urdu mein sawal poochna pasand karte hain. "
            "Tamam policies wohi hain jo upar English mein likhi hain."
        ),
        P("<b>Mera order kab tak pohnchay ga?</b>", BODY),
        P(
            "Standard delivery 3 se 5 business days mein hoti hai aur uski fees $4.99 hai. Express delivery 1 se 2 "
            "business days mein hoti hai ($9.99). $50 se ziada ke order par standard delivery free hai. "
            "Delivery ka waqt order ke Shipped hone ke baad shuru hota hai."
        ),
        P("<b>Mujhe order cancel karna hai, kya karun?</b>", BODY),
        P(
            "Order Shipped hone se pehle aap kisi bhi waqt cancel kar sakte hain. Address ya items 1 ghante ke "
            "andar free mein change ho sakte hain. Shipped hone ke baad cancel nahi hota, lekin aap parcel lene se "
            "inkar kar sakte hain ya delivery ke baad return kar sakte hain."
        ),
        P("<b>Item wapis kaise karun aur paise kab milenge?</b>", BODY),
        P(
            "Aam items 14 din ke andar wapis ho sakte hain, electronics 7 din ke andar. My Orders mein Return Item "
            "select karein. Kharab ya ghalat item ki shikayat 48 ghante ke andar karein, us mein pickup free hai. "
            "Mind badalne ki surat mein $3.99 return shipping kat'ti hai. Refund jaanch ke baad original payment "
            "method par wapis aata hai."
        ),
        P("<b>Password bhool gaya hun.</b>", BODY),
        P(
            "Login screen par Forgot Password dabayein. Reset link 15 minute tak valid rehta hai. 5 ghalat koshishon "
            "ke baad account 30 minute ke liye lock ho jata hai."
        ),
        P("<b>Support se kaise baat karun?</b>", BODY),
        P(
            "Live chat 24 ghante khuli hai. Helpline 0800-555-0199 par Peer se Hafta subah 9 se raat 9 baje tak "
            "milti hai (Itwaar ko 11 se 6 baje). Email: support@novamart.example, jawab 24 ghante mein milta hai."
        ),
        PageBreak(),
    ]

    # ---- Page 12: FAQ
    s += [
        P("12. Frequently asked questions", H1),
        grid(
            [
                ["Question", "Answer"],
                ["Is Cash on Delivery available for every order?", "COD is available for orders up to $200 only."],
                ["Can I change my delivery address after shipping?", "No. The address can be changed only before the order is Shipped."],
                ["How many delivery attempts does the courier make?", "Up to 3 attempts, after which the parcel returns to the warehouse."],
                ["Do I need the invoice for warranty?", "Yes, a valid NovaMart invoice is required for every warranty claim."],
                ["How long is the Forgot Password link valid?", "15 minutes."],
                ["Does a warranty replacement restart the warranty?", "No, the remaining warranty period continues."],
                ["Can I use two coupons on one order?", "No, only one coupon per order."],
                ["When do NovaPoints expire?", "12 months after they are earned."],
                ["Does NovaMart ask for my OTP or password?", "Never. Do not share them with anyone, including NovaMart staff."],
            ],
            [7.0, 10.0],
        ),
        Spacer(1, 0.6 * cm),
        P("Thank you for shopping with NovaMart. We are here to help, 24 hours a day on live chat.", SMALL),
    ]

    CS_PDF.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(CS_PDF), pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2.2 * cm, bottomMargin=2 * cm,
        title="NovaMart Customer Support Handbook", author="NovaMart (sample)",
    )
    doc.build(s, canvasmaker=canvas_with_header("NovaMart Customer Support Handbook  |  v3.2  |  For customers and support agents"))
    print("created", CS_PDF)


# =========================================================================== #
# 2) LEGAL: MASTER SERVICES AGREEMENT (fictional parties, neutral jurisdiction)
# =========================================================================== #
def build_legal() -> None:
    s: list = []

    # ---- Page 1: title + parties
    s += [
        Spacer(1, 1.0 * cm),
        P("MASTER SERVICES AGREEMENT", TITLE),
        P("between Alpha Tech Solutions Ltd. and Beta Retail Co.", SUB),
        P("Effective Date: 1 January 2026", SUB),
        Spacer(1, 0.5 * cm),
        P(
            "<i>Sample document: this agreement is fictional and was written only to demonstrate a document "
            "question-answering system. It is not legal advice and must not be signed or relied upon.</i>",
            SMALL,
        ),
        Spacer(1, 0.4 * cm),
        P("PARTIES", H1),
        P(
            "This Master Services Agreement (the <b>Agreement</b>) is made on the Effective Date between "
            "<b>Alpha Tech Solutions Ltd.</b>, a company that provides software development and managed IT "
            "services (the <b>Service Provider</b>), and <b>Beta Retail Co.</b>, a retail company (the "
            "<b>Client</b>). The Service Provider and the Client are each a <b>Party</b> and together the "
            "<b>Parties</b>."
        ),
        P("BACKGROUND", H1),
        P(
            "The Client wishes to appoint the Service Provider to design, build and operate an online "
            "ordering and inventory platform, and the Service Provider has agreed to provide these services "
            "on the terms of this Agreement."
        ),
        P("1. DEFINITIONS", H1),
        grid(
            [
                ["Term", "Meaning"],
                ["Services", "The software development, hosting, maintenance and support services described in Schedule A."],
                ["Deliverables", "All software, documents and materials that the Service Provider creates for the Client under this Agreement."],
                ["Confidential Information", "Any non-public information disclosed by one Party to the other, in any form, that is marked confidential or would reasonably be understood to be confidential."],
                ["Monthly Fee", "The recurring fee of $12,500 described in Clause 3."],
                ["Business Day", "Any day other than a Saturday, Sunday or public holiday in the place where the Service Provider is based."],
                ["Personal Data", "Any information relating to an identified or identifiable individual that is processed under this Agreement."],
            ],
            [4.2, 12.8],
        ),
        PageBreak(),
    ]

    # ---- Page 2: scope + term
    s += [
        P("2. SCOPE OF SERVICES", H1),
        P(
            "2.1 The Service Provider shall provide the Services in accordance with this Agreement, Schedule A "
            "(Services and Jurisdiction) and Schedule B (Service Levels)."
        ),
        P(
            "2.2 Any change to the Services must be agreed in writing in a change request signed by both "
            "Parties. The Service Provider is not required to perform extra work until a change request has "
            "been signed, and the fees for the extra work will be stated in the change request."
        ),
        P(
            "2.3 The Client shall provide timely access to information, systems and personnel that the Service "
            "Provider reasonably needs. The Service Provider is not responsible for delays caused by the "
            "Client's failure to do so."
        ),
        P("3. TERM", H1),
        P(
            "3.1 This Agreement starts on the Effective Date and continues for an <b>initial term of 24 months</b>."
        ),
        P(
            "3.2 After the initial term the Agreement renews automatically for successive periods of "
            "<b>12 months</b> unless either Party gives the other at least <b>60 days' written notice</b> "
            "before the end of the current term that it does not want to renew."
        ),
        P("4. FEES AND PAYMENT", H1),
        P(
            "4.1 <b>Monthly Fee.</b> The Client shall pay the Service Provider a Monthly Fee of <b>$12,500</b> "
            "for the operation, hosting and support of the platform. The Monthly Fee is invoiced monthly in advance."
        ),
        P(
            "4.2 <b>Implementation Fee.</b> The Client shall pay a one-time Implementation Fee of "
            "<b>$30,000</b> for the design and build of the platform, payable in three instalments linked to "
            "milestones, as shown in the Payment Milestones table below."
        ),
        PageBreak(),
    ]

    # ---- Page 3: payment (image with milestone table)
    pay_img = table_image(
        "Payment milestones - Implementation Fee ($30,000)",
        ["Milestone", "Share", "Amount", "Due"],
        [
            ["Signing of this Agreement", "40%", "$12,000", "On signing"],
            ["Platform go-live", "30%", "$9,000", "Within 15 days of go-live"],
            ["Final acceptance", "30%", "$9,000", "Within 15 days of acceptance"],
        ],
        [420, 160, 220, 400],
    )
    s += [
        KeepTogether([rl_image(pay_img, 16.5), P("Table 1 - Payment milestones for the Implementation Fee", CAPTION)]),
        P(
            "4.3 <b>Payment terms.</b> Undisputed invoices are payable within <b>30 days</b> of the invoice date "
            "(net 30). All fees are exclusive of taxes, which the Client shall pay where applicable."
        ),
        P(
            "4.4 <b>Late payment.</b> Overdue amounts carry interest at <b>1.5% per month</b> from the due date "
            "until payment. If an undisputed invoice is more than 30 days overdue, the Service Provider may "
            "suspend the Services after giving 10 days' written notice."
        ),
        P(
            "4.5 <b>Disputed invoices.</b> The Client shall notify the Service Provider of a disputed amount in "
            "writing within 14 days of the invoice date, pay the undisputed part, and the Parties shall work "
            "in good faith to resolve the dispute."
        ),
        P("5. INTELLECTUAL PROPERTY", H1),
        P(
            "5.1 Each Party keeps the intellectual property rights it owned before this Agreement. The "
            "Service Provider's pre-existing tools, libraries and methods remain the Service Provider's property."
        ),
        P(
            "5.2 The Client owns the Deliverables once the Client has paid the related fees in full. Until then "
            "the Client has a limited licence to use the Deliverables for its own business."
        ),
        P(
            "5.3 The Service Provider grants the Client a non-exclusive, perpetual, royalty-free licence to use "
            "any of the Service Provider's pre-existing materials that are built into the Deliverables, only "
            "as part of those Deliverables."
        ),
        PageBreak(),
    ]

    # ---- Page 4: confidentiality + warranties
    s += [
        P("6. CONFIDENTIALITY", H1),
        P(
            "6.1 Each Party shall keep the other Party's Confidential Information secret, use it only to perform "
            "this Agreement and share it only with employees and advisers who need to know it and are bound by "
            "similar duties."
        ),
        P(
            "6.2 The duty of confidentiality continues for <b>5 years</b> after this Agreement ends. Trade secrets "
            "stay protected for as long as they remain trade secrets."
        ),
        P("6.3 The duty does not apply to information that:"),
        bullets(
            [
                "is or becomes public without a breach of this Agreement;",
                "was already lawfully known to the receiving Party;",
                "is independently developed without use of the Confidential Information; or",
                "is lawfully received from a third party who has no duty of confidence.",
            ]
        ),
        P(
            "6.4 A Party may disclose Confidential Information when required by law or by a court, provided it "
            "gives the other Party prompt written notice (where the law allows) and discloses only what is required."
        ),
        P("7. WARRANTIES", H1),
        P(
            "7.1 The Service Provider warrants that it will perform the Services with reasonable skill and care, "
            "using suitably qualified personnel."
        ),
        P(
            "7.2 If the Services do not meet this warranty and the Client notifies the Service Provider within "
            "<b>90 days</b> of performance, the Service Provider shall re-perform the Services at no extra cost. "
            "This re-performance is the Client's only remedy for breach of the warranty in Clause 7.1."
        ),
        P(
            "7.3 Except as stated in this Agreement, the Service Provider gives no other warranties, express or "
            "implied, including that the platform will be completely error free or uninterrupted."
        ),
        PageBreak(),
    ]

    # ---- Page 5: liability + indemnity
    s += [
        P("8. LIMITATION OF LIABILITY", H1),
        P(
            "8.1 <b>Cap.</b> Each Party's total liability to the other under or in connection with this Agreement, "
            "whether in contract, tort (including negligence) or otherwise, is limited to the total fees paid "
            "and payable by the Client in the <b>12 months</b> before the event that gave rise to the claim."
        ),
        P(
            "8.2 <b>Excluded losses.</b> Neither Party is liable for indirect or consequential loss, loss of "
            "profit, loss of revenue, loss of goodwill or loss of anticipated savings."
        ),
        P("8.3 <b>Exceptions.</b> Clauses 8.1 and 8.2 do not limit liability for:"),
        bullets(
            [
                "death or personal injury caused by negligence;",
                "fraud or fraudulent misrepresentation;",
                "gross negligence or wilful misconduct;",
                "breach of the confidentiality obligations in Clause 6; or",
                "the indemnities in Clause 9.",
            ]
        ),
        P("9. INDEMNIFICATION", H1),
        P(
            "9.1 The Service Provider shall defend and indemnify the Client against third-party claims that the "
            "Deliverables, as provided by the Service Provider, infringe that third party's intellectual "
            "property rights. This does not apply to claims caused by changes made by the Client or by use of the "
            "Deliverables in combination with items the Service Provider did not supply."
        ),
        P(
            "9.2 The Client shall defend and indemnify the Service Provider against third-party claims arising "
            "from the data, content or instructions that the Client provides to the Service Provider."
        ),
        P(
            "9.3 The indemnified Party must give prompt written notice of a claim, allow the indemnifying Party "
            "to control the defence and settlement, and give reasonable help at the indemnifying Party's cost."
        ),
        PageBreak(),
    ]

    # ---- Page 6: data protection
    s += [
        P("10. DATA PROTECTION", H1),
        P(
            "10.1 The Service Provider shall process Personal Data only on the Client's documented instructions "
            "and only to provide the Services."
        ),
        P(
            "10.2 The Service Provider shall apply appropriate technical and organisational security measures, "
            "including encryption of Personal Data in transit and at rest and access controls limited to "
            "authorised staff."
        ),
        P(
            "10.3 The Service Provider shall notify the Client without undue delay and in any event within "
            "<b>72 hours</b> after becoming aware of a Personal Data breach, and shall give the information the "
            "Client reasonably needs to meet its own legal duties."
        ),
        P(
            "10.4 The Service Provider may use sub-processors only with the Client's prior written consent and "
            "remains responsible for their acts and omissions."
        ),
        P(
            "10.5 When the Agreement ends, the Service Provider shall, at the Client's choice, return or delete "
            "all Personal Data within <b>30 days</b> and confirm the deletion in writing."
        ),
        P("11. SERVICE LEVELS", H1),
        P(
            "11.1 The Service Provider shall make the platform available at least <b>99.5%</b> of the time in each "
            "calendar month, excluding planned maintenance announced at least 48 hours in advance. Response and "
            "resolution targets, and service credits for missed availability, are set out in Schedule B."
        ),
        P(
            "11.2 Service credits are the Client's sole financial remedy for failure to meet the availability "
            "target, without limiting the Client's right to terminate for repeated or serious failure under "
            "Clause 12."
        ),
        PageBreak(),
    ]

    # ---- Page 7: termination (image + text)
    term_img = table_image(
        "Termination at a glance",
        ["Type", "Who can use it", "Notice / cure period"],
        [
            ["For convenience", "Client", "90 days' written notice"],
            ["For convenience", "Service Provider", "180 days' written notice"],
            ["For material breach", "Either Party", "30 days to cure after notice"],
            ["For insolvency", "Either Party", "Immediate, by written notice"],
        ],
        [320, 340, 440],
    )
    s += [
        P("12. TERMINATION", H1),
        P(
            "12.1 <b>For convenience.</b> The Client may terminate this Agreement for convenience by giving the "
            "Service Provider 90 days' written notice. The Service Provider may terminate for convenience by "
            "giving the Client 180 days' written notice."
        ),
        P(
            "12.2 <b>Early termination fee.</b> If the Client terminates for convenience during the first 12 "
            "months after the Effective Date, the Client shall also pay an early termination fee equal to "
            "<b>three months' Monthly Fees</b> ($37,500)."
        ),
        P(
            "12.3 <b>For material breach.</b> Either Party may terminate this Agreement by written notice if the "
            "other Party commits a material breach and does not fix it within <b>30 days</b> after receiving "
            "written notice describing the breach."
        ),
        P(
            "12.4 <b>Insolvency.</b> Either Party may terminate immediately by written notice if the other Party "
            "becomes insolvent, enters bankruptcy, liquidation or administration, or stops carrying on business."
        ),
        KeepTogether([rl_image(term_img, 16), P("Table 2 - Termination rights and notice periods", CAPTION)]),
        P(
            "12.5 <b>Effect of termination.</b> On termination or expiry: (a) the Client shall pay all fees due up to "
            "the termination date; (b) each Party shall return or destroy the other's Confidential Information "
            "within <b>15 days</b>; (c) the Service Provider shall give reasonable help to move the platform to the "
            "Client or a new provider for up to 60 days, at its standard hourly rates; and (d) clauses that are "
            "meant to continue, including Clauses 5, 6, 8, 9 and 14, survive."
        ),
        PageBreak(),
    ]

    # ---- Page 8: force majeure, disputes
    s += [
        P("13. FORCE MAJEURE", H1),
        P(
            "13.1 Neither Party is liable for delay or failure caused by events beyond its reasonable control, "
            "such as natural disaster, war, terrorism, epidemic, government action, power or network failure "
            "not caused by the Party, or industrial action by people outside that Party (a <b>Force Majeure Event</b>), "
            "provided it notifies the other Party promptly and tries to reduce the effect."
        ),
        P(
            "13.2 If a Force Majeure Event continues for more than <b>30 consecutive days</b>, either Party may "
            "terminate this Agreement by written notice without liability for that termination. Payment "
            "obligations for Services already performed continue."
        ),
        P("14. GOVERNING LAW AND DISPUTE RESOLUTION", H1),
        P(
            "14.1 This Agreement and any dispute arising from it are governed by the law of the jurisdiction "
            "named in Schedule A (<b>Singapore</b>)."
        ),
        P("14.2 The Parties shall resolve disputes in this order:"),
        numbered(
            [
                "<b>Executive escalation:</b> senior executives of both Parties meet within 15 days after one Party gives a written dispute notice.",
                "<b>Mediation:</b> if not solved, the Parties try mediation with a mutually agreed mediator for up to 30 days.",
                "<b>Arbitration:</b> if still not solved, the dispute is finally decided by a single arbitrator. The seat of arbitration is Singapore and the language is English. The decision is final and binding.",
            ]
        ),
        P(
            "14.3 Either Party may ask a court at any time for urgent interim relief, for example to protect "
            "Confidential Information or intellectual property."
        ),
        P(
            "14.4 Each Party bears its own costs of mediation and arbitration, and the Parties share the fees of the "
            "mediator and arbitrator equally, unless the arbitrator decides otherwise."
        ),
        PageBreak(),
    ]

    # ---- Page 9: general provisions
    s += [
        P("15. GENERAL PROVISIONS", H1),
        P(
            "15.1 <b>Notices.</b> Notices must be in writing and sent by email to the address in Schedule A with a "
            "copy by courier to the Party's registered address. A notice is treated as received on the next "
            "Business Day after emailing, or 3 Business Days after being sent by courier."
        ),
        P(
            "15.2 <b>Assignment.</b> Neither Party may assign or transfer its rights or duties under this "
            "Agreement without the other Party's prior written consent, which must not be unreasonably "
            "withheld. A merger or sale of all or substantially all of a Party's business does not need consent "
            "if the new owner accepts this Agreement in writing."
        ),
        P(
            "15.3 <b>Independent contractors.</b> The Parties are independent contractors. Nothing in this "
            "Agreement creates a partnership, joint venture or employment relationship."
        ),
        P(
            "15.4 <b>Non-solicitation.</b> During this Agreement and for 12 months afterwards, neither Party shall "
            "directly solicit for employment any employee of the other Party who worked on the Services, "
            "except through general job advertisements."
        ),
        P(
            "15.5 <b>Entire agreement.</b> This Agreement, with its Schedules, is the entire agreement between the "
            "Parties on its subject and replaces all earlier discussions. Changes must be in writing and signed "
            "by both Parties."
        ),
        P(
            "15.6 <b>Severability and waiver.</b> If any part of this Agreement is found unenforceable, the rest "
            "continues. A delay in using a right is not a waiver of that right."
        ),
        P(
            "15.7 <b>Counterparts.</b> This Agreement may be signed in counterparts, including by electronic "
            "signature, and each counterpart is an original."
        ),
        PageBreak(),
    ]

    # ---- Page 10: schedules (SLA image)
    sla_img = table_image(
        "Schedule B - Service credits for missed availability",
        ["Monthly availability", "Service credit (share of Monthly Fee)"],
        [
            ["99.5% or higher", "No credit"],
            ["99.0% to 99.49%", "5% of the Monthly Fee"],
            ["98.0% to 98.99%", "10% of the Monthly Fee"],
            ["Below 98.0%", "20% of the Monthly Fee (maximum)"],
        ],
        [420, 700],
    )
    resp_img = table_image(
        "Schedule B - Support response and resolution targets",
        ["Severity", "Description", "Response", "Resolution target"],
        [
            ["Severity 1", "Platform down / critical", "1 hour", "4 hours"],
            ["Severity 2", "Major feature impaired", "4 hours", "1 Business Day"],
            ["Severity 3", "Minor issue / question", "1 Business Day", "5 Business Days"],
        ],
        [190, 410, 220, 280],
    )
    s += [
        P("SCHEDULE A - SERVICES AND JURISDICTION", H1),
        grid(
            [
                ["Item", "Details"],
                ["Services", "Design, build, hosting, maintenance and support of the Client's online ordering and inventory platform."],
                ["Governing law and seat of arbitration", "Singapore"],
                ["Service Provider notice email", "legal@alphatech.example"],
                ["Client notice email", "contracts@betaretail.example"],
                ["Initial term", "24 months from 1 January 2026"],
            ],
            [5.5, 11.5],
        ),
        Spacer(1, 0.3 * cm),
        P("SCHEDULE B - SERVICE LEVELS", H1),
        P("Availability is measured per calendar month. If it falls below 99.5%, the Client receives service credits against the next invoice."),
        KeepTogether([rl_image(sla_img, 16), P("Table 3 - Service credits", CAPTION)]),
        KeepTogether([rl_image(resp_img, 16), P("Table 4 - Response and resolution targets", CAPTION)]),
        PageBreak(),
    ]

    # ---- Page 11: signatures + plain-language summary (English)
    s += [
        P("SIGNATURES", H1),
        P("Signed by the authorised representatives of the Parties on the Effective Date."),
        grid(
            [
                ["For Alpha Tech Solutions Ltd.", "For Beta Retail Co."],
                ["Name: ______________________<br/>Title: ______________________<br/>Signature: ________________<br/>Date: ______________________",
                 "Name: ______________________<br/>Title: ______________________<br/>Signature: ________________<br/>Date: ______________________"],
            ],
            [8.5, 8.5],
        ),
        Spacer(1, 0.5 * cm),
        P("PLAIN-LANGUAGE SUMMARY (not part of the legal text)", H1),
        bullets(
            [
                "The deal runs for 24 months and renews yearly unless a Party gives 60 days' notice.",
                "The Client pays $12,500 every month plus a one-time $30,000 build fee paid in three milestones.",
                "Invoices are due in 30 days. Late payments cost 1.5% per month.",
                "The Client owns what is built once it has paid for it.",
                "Secrets stay secret for 5 years after the Agreement ends.",
                "Each Party's liability is capped at 12 months of fees, with exceptions such as fraud and confidentiality breaches.",
                "The Client can leave with 90 days' notice (plus an early fee in year 1); the Service Provider needs 180 days' notice.",
                "A breach must be fixed within 30 days or the other side can terminate.",
                "Disputes go to executives, then mediation, then arbitration in Singapore.",
            ]
        ),
        PageBreak(),
    ]

    # ---- Page 12: Roman Urdu summary
    s += [
        P("MUKHTASAR KHULASA - ROMAN URDU (qanooni matan ka hissa nahi)", H1),
        P(
            "<b>Muddat:</b> Ye muaahida 1 January 2026 se shuru hota hai aur pehli muddat 24 mahine hai. "
            "Is ke baad ye khud-ba-khud 12 mahine ke liye renew hota rehta hai, agar koi fareeq muddat khatam "
            "hone se kam az kam 60 din pehle likhit notice de kar renew na karna chahe."
        ),
        P(
            "<b>Fees:</b> Client ko har mahine $12,500 Monthly Fee deni hoti hai jo mahine ke shuru mein invoice hoti hai. "
            "Is ke ilawa $30,000 ki ek martaba Implementation Fee hai jo teen kistoon mein di jati hai: dastkhat par 40% ($12,000), "
            "go-live par 30% ($9,000) aur final acceptance par 30% ($9,000). Invoice 30 din mein ada karna zaroori hai. "
            "Der se payment par 1.5% mahana sood lagta hai."
        ),
        P(
            "<b>Khatma (Termination):</b> Client 90 din ke likhit notice se muaahida khatam kar sakta hai. Agar ye pehle 12 mahine "
            "ke andar kare to teen mahine ki Monthly Fee yani $37,500 early termination fee deni hogi. Service Provider ko 180 din "
            "ka notice dena hota hai. Agar koi fareeq bari khilaf-warzi (material breach) kare aur 30 din mein theek na kare to "
            "doosra fareeq muaahida khatam kar sakta hai.",
        ),
        P(
            "<b>Raazdari (Confidentiality):</b> Khufia maloomat muaahida khatam hone ke baad bhi 5 saal tak raaz rehti hain."
        ),
        P(
            "<b>Zimmedari ki had:</b> Har fareeq ki kul zimmedari pichle 12 mahine ki fees tak mehdood hai. Dhoka, jaan ka nuqsan, "
            "gross negligence, raazdari ki khilaf-warzi aur indemnity is had se bahar hain."
        ),
        P(
            "<b>Jhagre ka hal:</b> Pehle dono taraf ke senior executives 15 din mein milte hain, phir 30 din tak mediation hoti hai, "
            "aur akhir mein ek arbitrator faisla karta hai. Arbitration ki jagah Singapore hai aur zubaan English hai."
        ),
        P(
            "<b>Data protection:</b> Service Provider ko data breach ki ittila 72 ghante ke andar deni hoti hai. Muaahida khatam hone "
            "par personal data 30 din ke andar wapis ya delete karna hota hai."
        ),
        Spacer(1, 0.4 * cm),
        P(
            "Note: Ye khulasa sirf samajhne ke liye hai. Qanooni faisle se pehle kisi wakeel (lawyer) se mashwara zaroor lein.",
            SMALL,
        ),
    ]

    LEGAL_PDF.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(LEGAL_PDF), pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2.2 * cm, bottomMargin=2 * cm,
        title="Master Services Agreement (sample)", author="Sample",
    )
    doc.build(s, canvasmaker=canvas_with_header("Master Services Agreement  |  Alpha Tech Solutions Ltd. and Beta Retail Co.  |  CONFIDENTIAL (sample)"))
    print("created", LEGAL_PDF)


if __name__ == "__main__":
    build_customer_support()
    build_legal()
