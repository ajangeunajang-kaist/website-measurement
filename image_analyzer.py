"""
image_analyzer.py
스크린샷 이미지에서 Colorfulness 지표 추출

측정 지표:
  - Top 3 dominant colors (K-means k=3)
  - Average saturation overall  / of dominant colors
  - Average brightness overall  / of dominant colors
"""

import colorsys
import numpy as np
from pathlib import Path
from PIL import Image
from sklearn.cluster import KMeans


def analyze_screenshot(screenshot_path: str, device_scale: float = 2.0) -> dict:
    """
    스크린샷을 분석하여 Colorfulness 지표와 페이지 높이 반환

    Args:
        screenshot_path: 스크린샷 파일 경로
        device_scale:    캡처 시 device_scale_factor (기본 2.0 = Retina)

    Returns:
        {
          dominant_color_1/2/3: hex string,
          avg_saturation_overall, avg_saturation_dominant,
          avg_brightness_overall, avg_brightness_dominant,
          screenshot_height_css_px: 스크린샷 세로 길이를 CSS px로 환산 (교차검증용)
        }
    """
    result = {}

    try:
        img = Image.open(screenshot_path).convert("RGB")
    except Exception as e:
        return {"image_error": str(e)}

    # 교차검증용: 스크린샷 세로 → CSS px 환산
    raw_h, raw_w = img.height, img.width
    result["screenshot_height_css_px"] = round(raw_h / device_scale)
    result["screenshot_width_css_px"]  = round(raw_w / device_scale)

    # ── 성능을 위해 이미지 리사이즈 ───────────────────────
    MAX_W = 800
    if img.width > MAX_W:
        ratio = MAX_W / img.width
        img = img.resize((MAX_W, int(img.height * ratio)), Image.LANCZOS)

    arr    = np.array(img, dtype=np.float32)
    pixels = arr.reshape(-1, 3)   # (N, 3)  RGB 0-255

    # ── 픽셀 샘플링 ──────────────────────────────────────
    n          = len(pixels)
    sample_n   = min(n, 15_000)
    rng        = np.random.default_rng(42)
    idx        = rng.choice(n, size=sample_n, replace=False)
    sample_rgb = pixels[idx]

    # ── K-means dominant colors (k=3) ────────────────────
    kmeans = KMeans(n_clusters=3, n_init=10, random_state=42, max_iter=300)
    kmeans.fit(sample_rgb)

    # 클러스터 크기 순(내림차순)으로 정렬
    labels    = kmeans.labels_
    counts    = np.bincount(labels, minlength=3)
    order     = np.argsort(-counts)
    centroids = kmeans.cluster_centers_[order]  # (3, 3) RGB

    for i, (r, g, b) in enumerate(centroids, start=1):
        result[f"dominant_color_{i}"] = "#{:02x}{:02x}{:02x}".format(
            int(np.clip(r, 0, 255)),
            int(np.clip(g, 0, 255)),
            int(np.clip(b, 0, 255)),
        )

    # ── 전체 픽셀 HSV ─────────────────────────────────────
    hsv_all = np.array(
        [colorsys.rgb_to_hsv(r / 255, g / 255, b / 255) for r, g, b in sample_rgb]
    )
    result["avg_saturation_overall"] = float(np.mean(hsv_all[:, 1]))
    result["avg_brightness_overall"] = float(np.mean(hsv_all[:, 2]))

    # ── dominant color centroid HSV ───────────────────────
    centroid_hsv = np.array(
        [colorsys.rgb_to_hsv(r / 255, g / 255, b / 255) for r, g, b in centroids]
    )
    result["avg_saturation_dominant"] = float(np.mean(centroid_hsv[:, 1]))
    result["avg_brightness_dominant"] = float(np.mean(centroid_hsv[:, 2]))

    return result
