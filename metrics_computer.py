"""
metrics_computer.py
DOM 추출 데이터와 이미지 분석 결과를 받아 최종 32개 지표 계산

(1) Visual Complexity  : 10개
(2) Colorfulness       : 10개
(3) Typography         : 12개 (Page Length 포함)
"""

from __future__ import annotations

# ─── Image type heuristics ────────────────────────────────────────────────────

IMAGE_TYPES = [
    "drawings",
    "pictures",
    "posters/infographics",
    "QR codes",
    "logos",
    "icons",
    "other",
]


def _classify_image(img: dict) -> str:
    """img 태그 메타데이터로 7가지 이미지 타입 중 하나를 반환 (휴리스틱)."""
    src        = img.get("src", "").lower()
    alt        = img.get("alt", "").lower()
    cls        = img.get("class_name", "").lower()
    id_        = img.get("id", "").lower()
    parent_tag = img.get("parent_tag", "").lower()
    parent_cls = img.get("parent_class", "").lower()
    gp_tag     = img.get("grandparent_tag", "").lower()
    all_text   = f"{src} {alt} {cls} {id_} {parent_cls}"

    w = img.get("rendered_w", 0) or img.get("natural_w", 0)
    h = img.get("rendered_h", 0) or img.get("natural_h", 0)

    # QR 코드
    if any(k in all_text for k in ["qr", "qrcode", "qr-code", "qr_code"]):
        return "QR codes"

    # 로고
    if any(k in all_text for k in ["logo", "brand", "emblem", "wordmark", "symbol"]):
        return "logos"
    if parent_tag in ("header", "nav") and 0 < w < 300 and 0 < h < 150:
        return "logos"
    if gp_tag in ("header", "nav") and 0 < w < 250 and 0 < h < 150:
        return "logos"

    # 아이콘
    if any(k in all_text for k in ["icon", "ico-", "sprite", "pictogram", "fa-", "glyphicon", "bi-"]):
        return "icons"
    if src.endswith(".svg") and 0 < w <= 80 and 0 < h <= 80:
        return "icons"
    if 0 < w <= 48 and 0 < h <= 48:
        return "icons"

    # 포스터/인포그래픽
    if any(k in all_text for k in ["poster", "infographic", "flyer", "exhibition",
                                    "exhibit", "전시", "포스터", "banner-img"]):
        return "posters/infographics"
    if w > 0 and h > 0 and (h / w) > 1.3 and w > 100:   # 세로가 훨씬 긴 이미지
        return "posters/infographics"

    # 사진 (photographs)
    if src.endswith((".jpg", ".jpeg", ".webp")) and w > 100 and h > 50:
        return "pictures"

    # 드로잉 (그래픽, 일러스트, GIF)
    if src.endswith((".svg", ".gif")) and (w > 80 or h > 80):
        return "drawings"
    if src.endswith(".png") and w > 80 and h > 50:
        return "drawings"

    return "other"


# ─── Visual Complexity ────────────────────────────────────────────────────────

def compute_visual_complexity(dom: dict) -> dict:
    """
    (1) Visual Complexity 10개 지표를 계산합니다.

    Returns keys:
      image_diversity, has_autoplay_effects, has_video_content,
      visual_density, autoplay_density,
      titles_per_1000w, headings_per_1000w, paragraphs_per_1000w,
      main_blocks_per_1000px, peripheral_blocks_per_1000px
    """
    page_h    = max(dom.get("page_height", 1), 1)
    word_cnt  = max(dom.get("word_count",  1), 1)

    images = dom.get("images", [])
    videos = dom.get("videos", [])

    # 이미지 타입 분류
    type_counts: dict[str, int] = {t: 0 for t in IMAGE_TYPES}
    for img in images:
        t = _classify_image(img)
        type_counts[t] += 1

    # autoplay
    autoplay_n = (
        dom.get("autoplay_carousel_count", 0)
        + sum(1 for v in videos if v.get("has_autoplay", False))
    )

    total_media = len(images) + len(videos)
    h_total     = sum(dom.get(f"h{i}", 0) for i in range(1, 6))

    return {
        # Diversity: 7가지 유형 중 존재하는 유형 수 (0–7)
        "image_diversity":               sum(1 for v in type_counts.values() if v > 0),
        # Autoplay / Video (binary)
        "has_autoplay_effects":          int(autoplay_n > 0),
        "has_video_content":             int(len(videos) > 0),
        # Density
        "visual_density":                total_media / page_h * 1000,
        "autoplay_density":              autoplay_n  / page_h * 1000,
        # Text organization (÷ 1,000 단어)
        "titles_per_1000w":              dom.get("title_count", 0) / word_cnt * 1000,
        "headings_per_1000w":            h_total                   / word_cnt * 1000,
        "paragraphs_per_1000w":          dom.get("p_count", 0)     / word_cnt * 1000,
        # Content blocks (÷ 1,000 px)
        "main_blocks_per_1000px":        dom.get("main_block_count",       0) / page_h * 1000,
        "peripheral_blocks_per_1000px":  dom.get("peripheral_block_count", 0) / page_h * 1000,
    }


# ─── Colorfulness ─────────────────────────────────────────────────────────────

def compute_colorfulness(dom: dict, img: dict) -> dict:
    """
    (2) Colorfulness 10개 지표를 계산합니다.

    Returns keys:
      bg_colors_count, text_colors_count, border_colors_count,
      dominant_color_1/2/3,
      avg_saturation_overall, avg_saturation_dominant,
      avg_brightness_overall, avg_brightness_dominant
    """
    return {
        # Color quantity (DOM/CSS 기반)
        "bg_colors_count":           len(dom.get("bg_colors",     [])),
        "text_colors_count":         len(dom.get("text_colors",   [])),
        "border_colors_count":       len(dom.get("border_colors", [])),
        # Dominant colors (스크린샷 K-means 기반)
        "dominant_color_1":          img.get("dominant_color_1", ""),
        "dominant_color_2":          img.get("dominant_color_2", ""),
        "dominant_color_3":          img.get("dominant_color_3", ""),
        # Saturation (스크린샷 HSV 기반)
        "avg_saturation_overall":    img.get("avg_saturation_overall",   None),
        "avg_saturation_dominant":   img.get("avg_saturation_dominant",  None),
        # Brightness (스크린샷 HSV 기반)
        "avg_brightness_overall":    img.get("avg_brightness_overall",   None),
        "avg_brightness_dominant":   img.get("avg_brightness_dominant",  None),
    }


# ─── Typography ───────────────────────────────────────────────────────────────

def compute_typography(dom: dict, img: dict) -> dict:
    """
    (3) Typography 10개 지표를 계산합니다.

    Returns keys:
      amount_of_text, page_length_px,
      luminance_contrast, font_size_weighted, line_spacing_weighted,
      sans_serif_ratio, bold_ratio, italic_ratio, underlined_ratio,
      headers_ratio, column_width_weighted, left_alignment_ratio

    * page_length_px: DOM scrollHeight (canonical).
    * page_length_cross_check: 스크린샷 높이를 CSS px로 환산한 값 (교차검증).
    * page_length_match: 두 값이 10% 이내이면 True.
    """
    items       = dom.get("text_items", [])
    total_chars = dom.get("total_chars", 0)          # 실제 값 그대로 (0 허용)
    _chars_denom = max(total_chars, 1)               # 분모 전용
    dom_page_h  = dom.get("page_height", 0)

    # ── 교차검증: Page Length ──────────────────────────────
    ss_h   = img.get("screenshot_height_css_px")   # 스크린샷 → CSS px
    if ss_h and dom_page_h:
        diff_ratio = abs(dom_page_h - ss_h) / max(dom_page_h, 1)
        page_match = diff_ratio < 0.10              # 10% 이내면 일치
    else:
        page_match = None

    base = {
        "amount_of_text":          total_chars,      # 0이면 DOM 미수집
        "page_length_px":          dom_page_h,          # canonical (DOM)
        "page_length_screenshot":  ss_h,                # 교차검증용
        "page_length_match":       page_match,          # True/False/None
    }

    if not items:
        return {
            **base,
            "luminance_contrast":    None,
            "font_size_weighted":    None,
            "line_spacing_weighted": None,
            "sans_serif_ratio":      None,
            "bold_ratio":            None,
            "italic_ratio":          None,
            "underlined_ratio":      None,
            "headers_ratio":         None,
            "column_width_weighted": None,
            "left_alignment_ratio":  None,
        }

    total_w = max(sum(it.get("chars", 0) for it in items), 1)  # 분모 전용

    def w_avg(key: str, cond=None) -> float | None:
        """chars 가중 평균. cond(item)=True 인 항목만."""
        sub = [it for it in items if cond(it)] if cond else items
        wt  = sum(it["chars"] for it in sub)
        if wt == 0:
            return None
        return sum(it["chars"] * (it.get(key) or 0) for it in sub) / wt

    def ratio(flag_key: str, truthy_values: tuple | None = None) -> float:
        """flag_key 가 True (또는 truthy_values 에 해당)인 chars 비율."""
        if truthy_values:
            n = sum(it["chars"] for it in items if it.get(flag_key) in truthy_values)
        else:
            n = sum(it["chars"] for it in items if it.get(flag_key))
        return n / total_w

    # Luminance contrast: contrast 값이 있는 항목만
    contrast_items = [it for it in items if it.get("contrast") is not None]
    if contrast_items:
        cw = sum(it["chars"] for it in contrast_items)
        luminance_contrast = (
            sum(it["chars"] * it["contrast"] for it in contrast_items) / cw
            if cw > 0 else None
        )
    else:
        luminance_contrast = None

    # Column width: max_chars_per_line > 0 인 항목만
    cw_items = [it for it in items if it.get("max_chars_per_line", 0) > 0]
    cw_total  = sum(it["chars"] for it in cw_items)
    col_width = (
        sum(it["chars"] * it["max_chars_per_line"] for it in cw_items) / cw_total
        if cw_total > 0 else None
    )

    return {
        **base,
        "luminance_contrast":    luminance_contrast,
        "font_size_weighted":    w_avg("font_size"),
        "line_spacing_weighted": w_avg("space_ratio"),
        "sans_serif_ratio":      ratio("is_sans_serif"),
        "bold_ratio":            ratio("is_bold"),
        "italic_ratio":          ratio("is_italic"),
        "underlined_ratio":      ratio("is_underline"),
        "headers_ratio":         ratio("is_header"),
        "column_width_weighted": col_width,
        "left_alignment_ratio":  ratio("text_align", truthy_values=("left", "start")),
    }


# ─── All metrics ──────────────────────────────────────────────────────────────

def compute_all_metrics(dom: dict, img_metrics: dict) -> dict:
    """세 그룹 지표를 모두 계산하여 단일 dict로 반환."""
    result = {}
    result.update(compute_visual_complexity(dom))
    result.update(compute_colorfulness(dom, img_metrics))
    result.update(compute_typography(dom, img_metrics))
    return result
