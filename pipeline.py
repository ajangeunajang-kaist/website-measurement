#!/usr/bin/env python3
"""
pipeline.py  –  웹사이트 디자인 정량 측정 자동화 파이프라인
=============================================================
문화예술 기관 웹사이트의 3개 범주 30개 지표 자동 측정

  (1) Visual Complexity  10개
  (2) Colorfulness       10개  (스크린샷 필요)
  (3) Typography         10개

입력:
    스크린샷 폴더  (파일명 형식: 기관명_도메인.png)
    예) 국립현대미술관_mmca.go.kr.png

출력:
    results.csv       최종 지표 CSV (사이트당 1행)
    dom_data/         DOM 추출 중간 결과 JSON
    pipeline_log.txt  진행 로그

사용법:
    python3 pipeline.py [--screenshots screenshots] [--output results.csv]
    python3 pipeline.py --analyze-only   # DOM 재수집 없이 기존 JSON+이미지만 분석
    python3 pipeline.py --fetch-only     # DOM JSON만 재수집, CSV 생성 안 함

의존성:
    pip install playwright pillow scikit-learn numpy openpyxl
    playwright install chromium
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from image_analyzer import analyze_screenshot
from metrics_computer import compute_all_metrics

# ── 기본 설정 ────────────────────────────────────────────────────────────────
DEFAULT_SS_DIR   = Path("screenshots")
DEFAULT_DOM_DIR  = Path("dom_data")
DEFAULT_OUT_CSV  = Path("results.csv")
LOG_FILE         = Path("pipeline_log.txt")
FAILED_FILE      = Path("failed.txt")

TIMEOUT_MS       = 30_000     # 페이지 로딩 타임아웃 (ms)
WAIT_MS          = 3_000      # 렌더링 대기 (ms)
JS_TIMEOUT_MS    = 60_000     # DOM 추출 JS 최대 실행 시간 (ms)
MAX_TEXT_ITEMS   = 5_000      # JS에서 수집할 최대 텍스트 노드 수 (성능 제한)
VIEWPORT         = {"width": 1440, "height": 900}
DEVICE_SCALE     = 2.0        # screenshot_batch.py 와 동일

# ── 팝업 제거 (screenshot_batch.py 동일) ──────────────────────────────────────
_POPUP_SELS = [
    "[class*='cookie']","[id*='cookie']","[class*='gdpr']","[id*='gdpr']",
    "[class*='modal']","[id*='modal']","[class*='popup']","[id*='popup']",
    "[class*='overlay']","[id*='overlay']","[class*='banner']","[id*='banner']",
    "[class*='toast']","[id*='toast']","[class*='dialog']","[id*='dialog']",
    "[class*='layer']","[id*='layer']","[class*='notice']","[id*='notice']",
    "[class*='alert']","[id*='alert']",
]
REMOVE_POPUP_JS = (
    "() => { const s="
    + json.dumps(_POPUP_SELS)
    + """;
    s.forEach(sel => {
        document.querySelectorAll(sel).forEach(el => {
            const st = window.getComputedStyle(el);
            const zi = parseInt(st.zIndex) || 0;
            if ((st.position === 'fixed' || st.position === 'absolute') && zi > 10)
                el.remove();
        });
    });
    document.body.style.overflow = 'auto';
    document.documentElement.style.overflow = 'auto'; }"""
)

# ── DOM 추출 JavaScript ───────────────────────────────────────────────────────
DOM_EXTRACT_JS = f"""
() => {{
const MAX_ITEMS = {MAX_TEXT_ITEMS};

// ── Helpers ──────────────────────────────────────────────
function parseRGB(s) {{
  if (!s) return null;
  const m = s.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
  return m ? [+m[1], +m[2], +m[3]] : null;
}}
function rgbToHex(rgb) {{
  if (!rgb) return null;
  return '#' + rgb.map(c => Math.round(c).toString(16).padStart(2,'0')).join('');
}}
function isTransparent(s) {{
  if (!s || s === 'transparent') return true;
  const m = s.match(/rgba\\([\\d\\s,]+,\\s*([\\d.]+)\\)/);
  return m ? +m[1] < 0.01 : false;
}}
function sRGB(c) {{
  c /= 255;
  return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}}
function lum(rgb) {{
  return 0.2126 * sRGB(rgb[0]) + 0.7152 * sRGB(rgb[1]) + 0.0722 * sRGB(rgb[2]);
}}
function contrast(fg, bg) {{
  const L1 = lum(fg), L2 = lum(bg);
  const hi = Math.max(L1,L2), lo = Math.min(L1,L2);
  return (hi + 0.05) / (lo + 0.05);
}}
function effectiveBg(el) {{
  let cur = el;
  while (cur && cur !== document.documentElement) {{
    const st = window.getComputedStyle(cur);
    if (!isTransparent(st.backgroundColor)) return parseRGB(st.backgroundColor);
    cur = cur.parentElement;
  }}
  return [255, 255, 255];
}}

// ── Page metadata ─────────────────────────────────────────
const pageHeight = Math.max(
  document.documentElement.scrollHeight,
  document.body ? document.body.scrollHeight : 0
);

// ── Counts ───────────────────────────────────────────────
const titleCount = document.querySelectorAll('title').length;
const hCounts = [1,2,3,4,5].map(i => document.querySelectorAll('h'+i).length);
const pCount  = document.querySelectorAll('p').length;

// ── Text stats ───────────────────────────────────────────
const bodyText  = (document.body && document.body.innerText) || '';
const totalChars = bodyText.replace(/[\\s\\u00A0]/g, '').length;
const wordCount  = bodyText.trim() ? bodyText.trim().split(/\\s+/).length : 0;

// ── Images ───────────────────────────────────────────────
const images = [];
document.querySelectorAll('img').forEach(img => {{
  const rect = img.getBoundingClientRect();
  images.push({{
    src:         (img.getAttribute('src') || img.getAttribute('data-src') || '').toLowerCase(),
    alt:         (img.alt || '').toLowerCase(),
    natural_w:   img.naturalWidth  || 0,
    natural_h:   img.naturalHeight || 0,
    rendered_w:  rect.width,
    rendered_h:  rect.height,
    rect_top:    rect.top + window.scrollY,
    class_name:  (img.className || '').toLowerCase(),
    id:          (img.id || '').toLowerCase(),
    role:        (img.getAttribute('role') || '').toLowerCase(),
    parent_tag:  img.parentElement ? img.parentElement.tagName.toLowerCase() : '',
    parent_class: img.parentElement ? (img.parentElement.className || '').toLowerCase() : '',
    grandparent_tag: (img.parentElement && img.parentElement.parentElement)
                     ? img.parentElement.parentElement.tagName.toLowerCase() : '',
  }});
}});

// ── Videos ──────────────────────────────────────────────
const videos = [];
document.querySelectorAll('video').forEach(v => {{
  videos.push({{
    has_autoplay: v.hasAttribute('autoplay') || v.autoplay || false,
    src: v.src || '',
  }});
}});

// ── Autoplay carousels ───────────────────────────────────
let autoplayCarouselCount = 0;
['[data-autoplay]','[data-ride="carousel"]','.slick-initialized'].forEach(sel => {{
  try {{ autoplayCarouselCount += document.querySelectorAll(sel).length; }} catch(e) {{}}
}});
document.querySelectorAll('[class*="slide"],[class*="carousel"],[class*="swiper"]').forEach(el => {{
  const st = window.getComputedStyle(el);
  if (st.animationName && st.animationName !== 'none' && st.animationPlayState === 'running')
    autoplayCarouselCount++;
}});

// ── CSS color sets ───────────────────────────────────────
const bgSet = new Set(), txtSet = new Set(), bdSet = new Set();
document.querySelectorAll('*').forEach(el => {{
  try {{
    const st = window.getComputedStyle(el);
    if (!isTransparent(st.backgroundColor)) {{ const h=rgbToHex(parseRGB(st.backgroundColor)); if(h) bgSet.add(h); }}
    {{ const h=rgbToHex(parseRGB(st.color)); if(h) txtSet.add(h); }}
    ['borderTopColor','borderRightColor','borderBottomColor','borderLeftColor'].forEach(p => {{
      if (!isTransparent(st[p])) {{ const h=rgbToHex(parseRGB(st[p])); if(h) bdSet.add(h); }}
    }});
  }} catch(e) {{}}
}});

// ── Text items (typography) ──────────────────────────────
const textItems = [];
if (document.body) {{
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
  let node;
  while ((node = walker.nextNode()) && textItems.length < MAX_ITEMS) {{
    const text  = node.textContent || '';
    const chars = text.replace(/[\\s\\u00A0]/g, '').length;
    if (chars === 0) continue;
    const par = node.parentElement;
    if (!par) continue;
    const ptag = par.tagName.toLowerCase();
    if (['script','style','noscript'].includes(ptag)) continue;
    try {{
      const st = window.getComputedStyle(par);
      if (st.display==='none' || st.visibility==='hidden' || st.opacity==='0') continue;
      const rect = par.getBoundingClientRect();
      const fs   = parseFloat(st.fontSize) || 16;
      let   lh   = parseFloat(st.lineHeight);
      if (isNaN(lh) || st.lineHeight==='normal') lh = fs * 1.2;
      const spaceRatio = fs > 0 ? (lh - fs) / fs : 0;
      const isHeader   = /^h[1-6]$/.test(ptag);
      const fwNum      = parseInt(st.fontWeight) || (st.fontWeight==='bold'?700:400);
      const isBold     = fwNum >= 600;
      const isItalic   = st.fontStyle==='italic' || st.fontStyle==='oblique';
      const tdLine     = (st.textDecoration||'') + (st.textDecorationLine||'');
      const isUnderline = tdLine.includes('underline');
      const textAlign  = st.textAlign || 'left';
      const ff         = (st.fontFamily||'').toLowerCase();
      const isSansSerif = ff.includes('sans') || ff.includes('system') ||
                          ff.includes('malgun') || ff.includes('noto') ||
                          ff.includes('apple') || ff.includes('roboto') ||
                          (!ff.includes('serif') && !ff.includes('mono') &&
                           !ff.includes('cursive') && !ff.includes('fantasy'));
      const elemW = rect.width;
      // CJK 기준 0.6 계수 (한글 1글자 ≈ font-size px)
      const maxCPL = elemW > 0 && fs > 0 ? Math.round(elemW / (fs * 0.6)) : 0;
      const fgRGB = parseRGB(st.color);
      const bgRGB = effectiveBg(par);
      const ctr   = (fgRGB && bgRGB) ? contrast(fgRGB, bgRGB) : null;
      textItems.push({{
        chars, font_size:fs, line_height:lh, space_ratio:spaceRatio,
        is_sans_serif:isSansSerif, is_bold:isBold, is_italic:isItalic,
        is_underline:isUnderline, is_header:isHeader,
        text_align:textAlign, max_chars_per_line:maxCPL, contrast:ctr,
      }});
    }} catch(e) {{}}
  }}
}}

// ── Content blocks ───────────────────────────────────────
let mainBlockCount = 0;
const mainSels = ['main','[role="main"]','#main','#content','#container','.main','.content'];
let mainContainer = null;
for (const s of mainSels) {{ mainContainer = document.querySelector(s); if (mainContainer) break; }}
if (mainContainer) {{
  for (const child of mainContainer.children) {{
    const st = window.getComputedStyle(child);
    if (st.display !== 'none' && st.visibility !== 'hidden') mainBlockCount++;
  }}
}} else {{
  mainBlockCount = document.querySelectorAll('section,article').length;
}}

const peripheralSet = new Set();
['aside','[role="complementary"]','header','footer','nav',
 '[class*="sidebar"]','[class*="cookie"]','[class*="gdpr"]',
 '[class*="modal"]','[class*="popup"]','[class*="overlay"]',
 '[class*="notification"]','[class*="alert"]','[class*="toast"]',
 '[class*="banner"]','[class*="layer"]','[class*="notice"]'].forEach(sel => {{
  try {{ document.querySelectorAll(sel).forEach(el => {{
    if (window.getComputedStyle(el).display !== 'none') peripheralSet.add(el);
  }}); }} catch(e) {{}}
}});
document.querySelectorAll('*').forEach(el => {{
  const st = window.getComputedStyle(el);
  if ((st.position==='fixed'||st.position==='sticky') && st.display!=='none')
    peripheralSet.add(el);
}});

return {{
  page_height:            pageHeight,
  title_count:            titleCount,
  h1: hCounts[0], h2: hCounts[1], h3: hCounts[2], h4: hCounts[3], h5: hCounts[4],
  p_count:                pCount,
  total_chars:            totalChars,
  word_count:             wordCount,
  images,
  videos,
  autoplay_carousel_count: autoplayCarouselCount,
  bg_colors:              [...bgSet],
  text_colors:            [...txtSet],
  border_colors:          [...bdSet],
  text_items:             textItems,
  main_block_count:       mainBlockCount,
  peripheral_block_count: peripheralSet.size,
}};
}}
"""

# ── Utilities ─────────────────────────────────────────────────────────────────

def safe_filename(name: str, url: str) -> str:
    """screenshot_batch.py 와 동일한 파일명 생성 규칙."""
    domain   = url.replace("https://", "").replace("http://", "").split("/")[0]
    combined = f"{name}_{domain}"
    combined = re.sub(r'[\\/*?:"<>|]', "_", combined)
    return combined[:100] + ".png"


def parse_screenshot_filename(png_path: Path) -> tuple[str, str, int | None]:
    """
    '001_기관명_도메인.png' 또는 '기관명_도메인.png' → (기관명, 'https://도메인', id)
    앞의 숫자 접두사(001_ 형식)가 있으면 id로 반환.
    """
    stem = png_path.stem

    # 앞의 숫자 접두사 추출 (예: "001_")
    id_num: int | None = None
    m = re.match(r'^(\d+)_(.+)$', stem)
    if m:
        id_num = int(m.group(1))
        stem   = m.group(2)

    parts = stem.rsplit("_", maxsplit=1)
    if len(parts) == 2 and "." in parts[1]:
        name, domain = parts
        return name, f"https://{domain}", id_num
    return stem, "", id_num


def log(msg: str, log_path: Path = LOG_FILE):
    ts  = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── DOM 추출 (Playwright) ─────────────────────────────────────────────────────

def extract_dom(page, url: str) -> dict:
    """
    이미 로드된 page 에서 DOM 데이터를 추출합니다.
    실패 시 빈 dict 반환.
    """
    try:
        page.set_default_timeout(JS_TIMEOUT_MS)   # evaluate 포함 전체 타임아웃 설정
        data = page.evaluate(DOM_EXTRACT_JS)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        return {"js_error": str(e)}


def fetch_site(page, name: str, url: str, ss_path: Path, dom_path: Path,
               force_screenshot: bool = False) -> dict:
    """
    URL 로드 → 팝업 제거 → 스크린샷 촬영 → HTML 저장 → DOM 추출.
    Returns: dom_data dict (저장도 수행).
    """
    # 페이지 로드
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        page.wait_for_timeout(WAIT_MS)
    except PlaywrightTimeout:
        log(f"  ⚠ 타임아웃: {url}")
        return {"fetch_error": "timeout"}
    except Exception as e:
        log(f"  ✗ 로드 실패: {e}")
        return {"fetch_error": str(e)}

    # 팝업 제거
    try:
        page.evaluate(REMOVE_POPUP_JS)
        page.wait_for_timeout(300)
    except Exception:
        pass

    # 스크린샷 (존재하지 않거나 force 옵션)
    if force_screenshot or not ss_path.exists():
        try:
            page.screenshot(path=str(ss_path), full_page=True)
            log(f"  📸 스크린샷 저장: {ss_path.name}")
        except Exception as e:
            log(f"  ⚠ 스크린샷 실패: {e}")

    # 원본 HTML 저장 (렌더링 후 전체 DOM)
    html_dir = dom_path.parent.parent / "html_source"
    html_dir.mkdir(parents=True, exist_ok=True)
    html_path = html_dir / (dom_path.stem + ".html")
    try:
        html_path.write_text(page.content(), encoding="utf-8")
        log(f"  💾 HTML 저장: {html_path.name}")
    except Exception as e:
        log(f"  ⚠ HTML 저장 실패: {e}")

    # DOM 추출
    dom = extract_dom(page, url)
    dom["_url"]  = url
    dom["_name"] = name

    # JSON 저장
    dom_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dom_path, "w", encoding="utf-8") as f:
        json.dump(dom, f, ensure_ascii=False, separators=(",", ":"))

    return dom


# ── 메인 파이프라인 ───────────────────────────────────────────────────────────

def run_pipeline(
    ss_dir:       Path,
    dom_dir:      Path,
    out_csv:      Path,
    analyze_only: bool = False,
    fetch_only:   bool = False,
    force_refetch: bool = False,
):
    ss_dir.mkdir(parents=True, exist_ok=True)
    dom_dir.mkdir(parents=True, exist_ok=True)

    # 스크린샷 목록 수집
    png_files = sorted(ss_dir.glob("*.png")) + sorted(ss_dir.glob("*.jpg"))
    if not png_files:
        log(f"[오류] {ss_dir} 에 스크린샷이 없습니다.")
        sys.exit(1)

    log(f"발견된 스크린샷: {len(png_files)}개")

    # CSV 헤더 (첫 실행 시에만 작성)
    csv_exists = out_csv.exists()
    csv_f = open(out_csv, "a", newline="", encoding="utf-8-sig")
    csv_writer = None   # 첫 번째 행 처리 후 초기화

    failed_rows = []

    # ── Playwright 세션 ────────────────────────────────────
    _pw_instance = sync_playwright().start() if not analyze_only else None
    browser  = None
    context_ = None

    if _pw_instance:
        browser  = _pw_instance.chromium.launch(
            headless=True,
            args=["--disable-notifications", "--no-sandbox"],
        )
        context_ = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=DEVICE_SCALE,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
            permissions=[],
        )
        context_.route(
            "**/(ads|analytics|tracking|doubleclick|googlesyndication)/**",
            lambda route: route.abort(),
        )

    # ── 사이트별 처리 ──────────────────────────────────────
    for idx, png_path in enumerate(png_files, 1):
        name, url, site_id = parse_screenshot_filename(png_path)
        dom_path    = dom_dir / (png_path.stem + ".json")

        log(f"\n[{idx:>3}/{len(png_files)}] {name}  ({url or '(URL 미확인)'})")

        # ── DOM 수집 ───────────────────────────────────────
        dom: dict = {}

        if analyze_only:
            # DOM JSON 불러오기
            if dom_path.exists():
                with open(dom_path, encoding="utf-8") as f:
                    dom = json.load(f)
                log("  📂 DOM JSON 로드")
            else:
                log("  ⚠ DOM JSON 없음 — colorfulness 지표만 측정")
        else:
            # DOM 재수집 필요 여부
            need_fetch = force_refetch or not dom_path.exists()
            if not need_fetch:
                with open(dom_path, encoding="utf-8") as f:
                    dom = json.load(f)
                log("  📂 DOM JSON 재사용")
            elif url:
                page = context_.new_page()
                try:
                    dom = fetch_site(page, name, url, png_path, dom_path)
                except Exception as e:
                    log(f"  ✗ fetch 실패: {e}")
                    dom = {"fetch_error": str(e)}
                finally:
                    page.close()
            else:
                log("  ⚠ URL 파싱 실패 — DOM 수집 불가")

        if fetch_only:
            continue

        # ── 이미지 분석 ───────────────────────────────────
        img_metrics: dict = {}
        if png_path.exists():
            try:
                img_metrics = analyze_screenshot(str(png_path), device_scale=DEVICE_SCALE)
                log("  🎨 이미지 분석 완료")
            except Exception as e:
                log(f"  ⚠ 이미지 분석 실패: {e}")
                img_metrics = {"image_error": str(e)}
        else:
            log(f"  ⚠ 스크린샷 없음: {png_path}")

        # ── 지표 계산 ──────────────────────────────────────
        try:
            metrics = compute_all_metrics(dom, img_metrics)
        except Exception as e:
            log(f"  ✗ 지표 계산 실패: {e}\n{traceback.format_exc()}")
            failed_rows.append((name, url, str(e)))
            continue

        # 메타 정보
        row = {
            "id":         site_id if site_id is not None else idx,
            "name":       name,
            "url":        url,
            "screenshot": png_path.name,
            **metrics,
            # 에러 필드
            "fetch_error":  dom.get("fetch_error", ""),
            "image_error":  img_metrics.get("image_error", ""),
            "js_error":     dom.get("js_error", ""),
        }

        # CSV 헤더 초기화 (최초 1회)
        if csv_writer is None:
            fieldnames = list(row.keys())
            csv_writer = csv.DictWriter(csv_f, fieldnames=fieldnames, extrasaction="ignore")
            if not csv_exists:
                csv_writer.writeheader()

        csv_writer.writerow(row)
        csv_f.flush()
        log(f"  ✅ 완료  →  {out_csv}")

    # ── 정리 ──────────────────────────────────────────────
    csv_f.close()

    if browser:
        browser.close()
    if _pw_instance:
        _pw_instance.stop()

    if failed_rows:
        with open(FAILED_FILE, "w", encoding="utf-8") as f:
            f.write("기관명\tURL\t오류\n")
            for n, u, e in failed_rows:
                f.write(f"{n}\t{u}\t{e}\n")
        log(f"\n실패 목록: {FAILED_FILE}  ({len(failed_rows)}개)")

    log(f"\n{'='*50}")
    log(f"완료! 결과: {out_csv.resolve()}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="웹사이트 디자인 정량 측정 파이프라인"
    )
    ap.add_argument(
        "--screenshots", default=str(DEFAULT_SS_DIR),
        help=f"스크린샷 폴더 (기본: {DEFAULT_SS_DIR})",
    )
    ap.add_argument(
        "--dom-dir", default=str(DEFAULT_DOM_DIR),
        help=f"DOM JSON 저장 폴더 (기본: {DEFAULT_DOM_DIR})",
    )
    ap.add_argument(
        "--output", default=str(DEFAULT_OUT_CSV),
        help=f"출력 CSV 경로 (기본: {DEFAULT_OUT_CSV})",
    )
    ap.add_argument(
        "--analyze-only", action="store_true",
        help="Playwright 없이 기존 dom_data JSON + 스크린샷만으로 분석",
    )
    ap.add_argument(
        "--fetch-only", action="store_true",
        help="DOM JSON 수집만 수행 (CSV 생성 안 함)",
    )
    ap.add_argument(
        "--force-refetch", action="store_true",
        help="이미 dom_data JSON 이 있어도 재수집",
    )
    args = ap.parse_args()

    log(f"파이프라인 시작  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"스크린샷 폴더: {args.screenshots}")
    log(f"DOM 데이터 폴더: {args.dom_dir}")
    log(f"출력 CSV: {args.output}")

    run_pipeline(
        ss_dir        = Path(args.screenshots),
        dom_dir       = Path(args.dom_dir),
        out_csv       = Path(args.output),
        analyze_only  = args.analyze_only,
        fetch_only    = args.fetch_only,
        force_refetch = args.force_refetch,
    )


if __name__ == "__main__":
    main()
