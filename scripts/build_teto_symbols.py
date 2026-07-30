#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
"""Сборка 10 символов слота «Тето Брейнрот: Дрель-Хант» из исходных JPEG.

Запуск (из корня репозитория, в билд-шаги НЕ подключён — только вручную):

    python3 scripts/build_teto_symbols.py

Читает `docs/assets/teto-symbol-sources/*.jpg`, пишет
`miniapp/static/slots/teto/<symbol_id>.webp` (512x512, RGBA).

━━━ ПОЧЕМУ ЭТОТ СКРИПТ ВООБЩЕ СУЩЕСТВУЕТ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Исходники — десять случайных картинок из интернета (мемы, фан-арт, фото
плюшевой игрушки, скетч ручкой, 3D-рендер, фотожаба с собакой, два фрукта с
лицом Тето). Ни одного PNG с альфой, ни одного общего стиля, разрешения от
225x225 до 1600x1488. Изначально владелец хотел перерисовать всё в едином
стиле — это невозможно, поэтому цельность набора вытягивает ОДНА общая
обработка (кадрирование + обводка-стикер + римлайт + свечение + грейдинг),
а не единый рисунок. Скрипт — единственный источник правды о том, как из
этих исходников получаются шипнутые ассеты; без него набор нельзя
воспроизвести, а «поправить один символ» превратится в ручной фотошоп.

━━━ МАППИНГ symbol_id → КАРТИНКА СОГЛАСОВАН С ВЛАДЕЛЬЦЕМ БОТА ━━━━━━━━━━━━━

ВАЖНО ДЛЯ БУДУЩЕГО ЧИТАТЕЛЯ: соответствие symbol_id ↔ исходный файл
(закодировано в именах файлов в `docs/assets/teto-symbol-sources/`)
согласовано с владельцем репозитория в переписке и является ОКОНЧАТЕЛЬНЫМ.
Два маппинга выглядят «перепутанными» и провоцируют желание их исправить —
НЕ НАДО:

  * `teto_chimera` (HIGH) — это фотожаба «головы Тето на теле лысой собаки»
    (IMG_2116), а НЕ Цербер. Несмотря на название «химера» и на то, что у
    Цербера тоже много голов.
  * `baguette_cerberus` (SCATTER) — это официальный арт рыжей Тето с
    поднятыми руками и молниями (IMG_2119), а НЕ собака.

Любой «фикс» этих двух ID сломает договорённость и визуальную логику
пейтейбла (скаттер должен быть узнаваемо-«человеческим» и ярко-розовым,
химера — уродливой собакой из HIGH-группы).

Тиры берутся из `bot/services/teto_megablock.py` (LOW_SYMBOLS /
HIGH_SYMBOLS / SCATTER_ID / WILD_ID) — сервер остаётся источником правды,
здесь они продублированы только чтобы выбрать силу обработки.

━━━ РЕШЕНИЯ ПО КАЖДОЙ КАРТИНКЕ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Фон бывает трёх видов, и обработка выбирается по факту, а не «на всякий
случай одинаково»:

1. ВЫРЕЗАЕМЫЙ ПЛОСКИЙ ФОН (7 картинок: 5 белых, 1 чёрная, 1 голубая) —
   режим `key`. Ключевой момент: заливка идёт ЗАЛИВКОЙ ОТ ГРАНИЦЫ КАДРА
   (scipy.ndimage.label + компоненты, касающиеся рамки), а НЕ глобальным
   «этот пиксель похож на фон → он прозрачный». Глобальный тест пробивает
   дыры внутри субъекта, потому что:
     * у `skull_cringe` внутри черепа та же белая бумага, что и снаружи;
     * у `teto_chimera` и `golden_drill` лицо/кожа Тето почти белые, а у
       собаки почти белые когти;
     * у `teto_baguette_knight` фон чёрный, и тени на щите/волосах тоже
       почти чёрные;
     * у фруктов лицо Тето — белёсое пятно посреди плода.
   После заливки дырки внутри субъекта дополнительно закрываются
   `binary_fill_holes` (страховка от протечек через тонкие щели).

   Полупрозрачные пиксели края «раскрашиваются обратно» (un-premultiply:
   fg = (obs - (1-a)*bg) / a). Без этого белый фон остаётся плёнкой на
   кромке и на тёмном `--bg-dominant: #0d0a18` символ получает светлое
   гало — самый заметный признак дешёвого выреза.

2. НЕВЫРЕЗАЕМЫЙ ФОТО-/СЦЕННЫЙ ФОН (2 картинки) — режим `medallion`.
     * `teto_0401` — плюшевая Тето в бумажной короне Burger King, снятая в
       салоне автомобиля: за игрушкой лобовое стекло, торпеда, зелень за
       окном. Вырезать плюш по контуру мехового края без ручного роto —
       гарантированный «ореол из пикселей машины», поэтому вместо плохого
       выреза сделан осознанный медальон: скруглённый квадратный кадр с
       мягким растушёванным краем и виньеткой, и оставшийся фон читается
       как намеренная подложка портрета. Кадр (8,36)-(404,430) выбран так,
       чтобы отрезать вотермарку TikTok в правом нижнем углу (@username
       начинается с y≈437, «From TikTok» с y≈460 — кадр кончается на 430)
       и заодно поднять морду плюша ближе к центру медальона: корона BK
       занимает верх кадра и без сдвига перетягивала композицию.
     * `baguette_cerberus` — арт с глитч-/VHS-обработкой: тёмная сцена,
       развёртки, молнии, хроматические сдвиги. Фон здесь — часть
       произведения, вырезать нечего. Медальон + подъём яркости/контраста
       и розовый лифт теней (иначе скаттер утонет в почти таком же тёмном
       фоне интерфейса).

3. ВОТЕРМАРКА НА ЯБЛОКЕ (`drill_lollipop`) — надпись «applto» чёрным
   контуром в правом верхнем углу, bbox x316..414 / y39..75. Она
   ЗАМАЗЫВАЕТСЯ ЧИСТО БЕЛЫМ ДО КЕЙИНГА (фон там ровно 255,255,255, шва не
   видно). Порядок критичен: если сначала кейить, буквы — не-белые
   пиксели, значит они попадут в субъект, окажутся внутри альфа-bbox,
   вмёрзнут в готовый ассет и вдобавок сдвинут центрирование (bbox
   растянется до правого края кадра).

3a. ФОТО-ТЕНИ У ФРУКТОВ (`baguette_crumb`, `drill_lollipop`) — оба
   исходника это фото на белом столе, и мягкая серая тень плавно уходит в
   фон. Одним порогом её не убрать: поднять tolerance настолько, чтобы
   съесть плотное ядро тени, нельзя — белёсое лицо Тето на боку плода
   местами выходит на самый силуэт, и заливка утечёт внутрь плода. Поэтому
   тень снимается точечно: `desat_wipe` внутри явного бокса гасит только
   МАЛОНАСЫЩЕННЫЕ (серые) пиксели, а цветной бок плода в том же боксе
   остаётся. Иначе тень не просто «грязнит» кромку — она попадает в
   alpha-bbox (у груши он растягивался до самого края кадра 735x731),
   плод съезжает из центра и получается на треть мельче остальных.
   У груши вдобавок срезана снизу тёмная полоса столешницы (кроп до
   y=703): она темнее любого разумного tolerance и читалась как субъект.

3b. ОТОРВАННЫЕ КУСКИ (`teto_drill_rage`) — катакана-звуковые эффекты
   висят в кадре отдельными пятнами. Они выброшены (`min_frac`): на 96 px
   это серый мусор вокруг персонажа, а alpha-bbox они растягивали на всю
   ширину кадра. Важная деталь реализации: вместе с самим куском гасится
   и его полупрозрачная бахрома (dilate(solid) * alpha), иначе от
   выброшенных пятен остаются призрачные контуры — bbox не уменьшается, а
   в альфе появляются десятки дырок.

4. ЧЁРНО-БЕЛЫЙ СКЕТЧ РУЧКОЙ (`skull_cringe`) — отдельный случай.
   Если выбить белую бумагу в альфу, останется чистая ЧЁРНАЯ линия, и на
   `#0d0a18` символ станет невидимым. Инверсия штриха в «светлое на
   прозрачном» тоже плохой вариант: у рисунка плотная штриховка тенями, и
   после инверсии самые тёмные (самые важные) места станут самыми
   яркими — тональная логика скетча вывернется наизнанку, череп
   развалится в белое пятно.
   Поэтому выбран третий путь: бумага остаётся ПОДЛОЖКОЙ. Заливка от
   границы убирает бумагу только СНАРУЖИ силуэта рисунка, дальше силуэт
   «зашивается» морфологическим закрытием + fill_holes (у причёски-дрелей
   открытый контур, и без закрытия заливка прогрызает бахрому внутрь), а
   внутренняя бумага перекрашивается дуотоном ink→paper
   (#14101f → #d8d2e4 — не чистый белый, иначе LOW-символ по яркости
   спорил бы с холодно-белым рантом HIGH-символов). Получается светлый
   стикер с тёмной графикой — ровно то же прочтение, что у `.chip` в
   tokens.css (белая заливка + тёмная обводка + жёсткая тень), так что
   решение не выпадает из дизайн-системы. Плюс слева обрезаются 9 px
   тёмного края скана.

━━━ ЕДИНАЯ ОБРАБОТКА (то, что делает набор набором) ━━━━━━━━━━━━━━━━━━━━━━

Все десять символов после матирования проходят один и тот же конвейер на
холсте 1024x1024 (2x суперсэмплинг, финальный ресайз в 512 — так кромка
обводки и свечение получаются гладкими):

  1. Грейдинг (насыщенность/контраст/яркость) — сводит рисунок, фото и
     3D-рендер к одной «плотности».
  2. Верхний светлый градиент + нижняя контактная тень по маске субъекта —
     задаёт всем символам ОДНО направление света, чего в исходниках нет.
  3. Огибающая силуэта = blur(alpha) > 0.5. Именно по ней (а не по самой
     альфе) строятся обводка/тень/свечение: тонкие детали (штрихи
     motion blur у `golden_drill`, ахоге-антенка у всех чиби-Тето,
     остатки эффектов скорости у `teto_drill_rage`) не обрастают каждая
     своим контуром — вырубка идёт по «телу» символа, как у настоящего
     die-cut стикера. Сами детали при этом остаются: субъект рисуется
     ПОВЕРХ обводки, просто без собственного ранта.
  4. Тёмный кейлайн (#14101f) вплотную к силуэту + широкий цветной рант
     наружу от него = стикерная обводка, читаемая на любом фоне.
  5. Наружное мягкое свечение цветом тира + падающая тень со смещением —
     отрывают символ от плитки борда.

Иерархия тиров читается силой этой обработки, а не разными эффектами:

  LOW      узкий приглушённо-лиловый рант (#6f6a8c), почти без свечения,
           лёгкая десатурация → четыре низких символа визуально «дешёвые».
  HIGH     широкий холодно-белый рант + циановое свечение (--accent-cyan).
  HIGH+    `teto_0401` — самый премиальный: тёплый белый рант + ЗОЛОТОЕ
           волосяное кольцо снаружи + золотое свечение.
  WILD     `golden_drill` — золотой рант с вертикальным градиентом +
           двойное золотое свечение (--accent-yellow), самый яркий объект
           на борде.
  SCATTER  `baguette_cerberus` — розовый рант (--accent-pink) с ВНУТРЕННИМ
           белым волосяным кольцом (двойное кольцо ни с чем не спутать,
           скаттеры игрок считает глазами за доли секунды) + сильное
           розовое свечение + подъём яркости тёмного исходника.

Кадрирование одинаковое для всех: субъект вписан по длинной стороне в
FIT_RATIO=0.80 холста по РЕАЛЬНОМУ alpha-bbox (не по краям файла),
пропорции никогда не растягиваются, оставшиеся 10% с каждой стороны
уходят под обводку, свечение и тень. Апскейл ограничен ~2.4x (см. вывод
скрипта — печатает эффективный масштаб каждого символа), чтобы не
разгонять 225x225 `utau_note` в мыло; мягкий unsharp компенсирует ресайз.
"""
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageChops, ImageEnhance, ImageFilter
    from scipy import ndimage
except ImportError as exc:  # pragma: no cover - диагностика окружения
    sys.exit(f"Нужны numpy + Pillow + scipy: {exc}")

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "docs" / "assets" / "teto-symbol-sources"
OUT_DIR = ROOT / "miniapp" / "static" / "slots" / "teto"

OUT_SIZE = 512
SS = 2  # суперсэмплинг: работаем на 1024, отдаём 512
WORK = OUT_SIZE * SS
FIT_RATIO = 0.80  # доля холста под длинную сторону субъекта (одна для всех)
MAX_UPSCALE = 2.4  # предохранитель от мыла на мелких исходниках

INK = (0x14, 0x10, 0x1F)  # цвет кейлайна и «чернил» скетча

# ─── палитра из miniapp/src/lib/styles/tokens.css ────────────────────────────
ACCENT_PINK = (0xFF, 0x5B, 0x8D)
ACCENT_CYAN = (0x7B, 0xE6, 0xFF)
ACCENT_YELLOW = (0xFF, 0xD8, 0x4A)


@dataclass
class Tier:
    """Параметры общей обработки, различающиеся только силой."""

    keyline: float  # толщина тёмного кейлайна, px на холсте WORK
    rim: float  # толщина цветного ранта наружу от кейлайна
    rim_color: tuple[int, int, int]
    rim_color_bottom: tuple[int, int, int] | None = None  # вертикальный градиент
    glow: tuple[int, int, int] = ACCENT_CYAN
    glow_radius: float = 24.0
    glow_alpha: float = 0.35
    halo_radius: float = 0.0  # второе, широкое свечение (wild/scatter)
    halo_alpha: float = 0.0
    hairline: tuple[int, int, int] | None = None  # доп. волосяное кольцо
    hairline_px: float = 5.0
    hairline_inner: bool = False  # True → кольцо внутри ранта, не снаружи
    saturation: float = 1.0
    contrast: float = 1.0
    brightness: float = 1.0
    toplight: float = 0.10  # сила верхнего светлого градиента
    shadow_alpha: float = 0.5


TIERS: dict[str, Tier] = {
    "low": Tier(
        keyline=6, rim=11, rim_color=(0x6F, 0x6A, 0x8C),
        glow=(0x4A, 0x42, 0x66), glow_radius=18, glow_alpha=0.22,
        saturation=0.90, contrast=1.04, brightness=0.97, toplight=0.08,
        shadow_alpha=0.42,
    ),
    "high": Tier(
        keyline=6, rim=17, rim_color=(0xE8, 0xE4, 0xF5),
        glow=ACCENT_CYAN, glow_radius=32, glow_alpha=0.46,
        saturation=1.10, contrast=1.09, brightness=1.02, toplight=0.12,
        shadow_alpha=0.55,
    ),
    # teto_0401 — топовый HIGH, «самый премиальный из всех»
    "high_premium": Tier(
        keyline=6, rim=19, rim_color=(0xFF, 0xF6, 0xDD),
        glow=ACCENT_YELLOW, glow_radius=40, glow_alpha=0.52,
        halo_radius=76, halo_alpha=0.20,
        hairline=ACCENT_YELLOW, hairline_px=6,
        saturation=1.14, contrast=1.12, brightness=1.04, toplight=0.13,
        shadow_alpha=0.6,
    ),
    "wild": Tier(
        keyline=6, rim=21, rim_color=(0xFF, 0xEE, 0xAE),
        rim_color_bottom=(0xF2, 0x9E, 0x12), glow=ACCENT_YELLOW,
        glow_radius=44, glow_alpha=0.80, halo_radius=104, halo_alpha=0.34,
        saturation=1.16, contrast=1.10, brightness=1.03, toplight=0.13,
        shadow_alpha=0.6,
    ),
    "scatter": Tier(
        keyline=6, rim=21, rim_color=ACCENT_PINK,
        glow=ACCENT_PINK, glow_radius=44, glow_alpha=0.80,
        halo_radius=104, halo_alpha=0.34,
        hairline=(0xFF, 0xF2, 0xF6), hairline_px=6, hairline_inner=True,
        saturation=1.18, contrast=1.10, brightness=1.10, toplight=0.10,
        shadow_alpha=0.6,
    ),
}


@dataclass
class SymbolSpec:
    file: str
    tier: str
    mode: str  # 'key' | 'medallion'
    note: str
    crop: tuple[int, int, int, int] | None = None  # (l, t, r, b) до всего
    wipe: list[tuple[int, int, int, int]] = field(default_factory=list)
    # (l, t, r, b, sat_max, dist_max) — «умная» замазка фото-тени: только
    # серые (низкая насыщенность) пиксели внутри бокса, чтобы не съесть
    # цветной бок фрукта, попавший в тот же бокс
    desat_wipe: list[tuple[int, int, int, int, float, float]] = field(
        default_factory=list
    )
    # --- key ---
    tol_lo: float = 10.0  # ниже — гарантированно фон (alpha 0)
    tol_hi: float = 26.0  # выше — гарантированно субъект (alpha 1)
    flood_tol: float = 0.0  # 0 → = tol_hi; больше, если нужно съесть тень
    choke: tuple[float, float] = (0.0, 1.0)  # уровни альфы (поджим кромки)
    seal: int = 0  # радиус морфологического закрытия силуэта, px
    min_frac: float = 0.0  # выбросить отдельные куски мельче доли главного
    duotone: tuple[tuple[int, int, int], tuple[int, int, int]] | None = None
    # --- medallion ---
    feather: float = 9.0
    vignette: float = 0.0
    pink_lift: float = 0.0
    sharpen: float = 0.6


# ВНИМАНИЕ: маппинг symbol_id → файл согласован с владельцем, см. докстринг.
SYMBOLS: dict[str, SymbolSpec] = {
    # ── WILD ─────────────────────────────────────────────────────────────
    "golden_drill": SymbolSpec(
        file="golden_drill--IMG_2110.jpg", tier="wild", mode="key",
        note="чиби-Тето трясёт головой, дрели-твинтейлы в motion blur; "
             "фон ровно белый (шум рамки ±1.5), кейится тривиально",
        tol_lo=12, tol_hi=30,
    ),
    # ── SCATTER ──────────────────────────────────────────────────────────
    "baguette_cerberus": SymbolSpec(
        file="baguette_cerberus--IMG_2119.jpg", tier="scatter", mode="medallion",
        note="глитч-арт: тёмная сцена, VHS-развёртки, молнии — фон часть "
             "картинки, вырезать нечего → медальон + подъём яркости",
        crop=(0, 16, 335, 376), feather=11, vignette=0.34, pink_lift=0.16,
    ),
    # ── HIGH ─────────────────────────────────────────────────────────────
    "teto_0401": SymbolSpec(
        file="teto_0401--IMG_2115.jpg", tier="high_premium", mode="medallion",
        note="фото плюша в короне BK в салоне авто; меховой край по фону "
             "не вырезать → медальон, кадр обрезает вотермарку TikTok "
             "(@username с y=437, «From TikTok» с y=460) и ставит морду "
             "плюша ближе к центру медальона",
        crop=(8, 36, 404, 430), feather=10, vignette=0.30,
    ),
    "teto_baguette_knight": SymbolSpec(
        file="teto_baguette_knight--IMG_2114.jpg", tier="high", mode="key",
        note="3D-рендер на чёрном; тени на щите и волосах почти чёрные, "
             "поэтому tol держим низким и опираемся на заливку от границы",
        tol_lo=14, tol_hi=34,
    ),
    "teto_drill_rage": SymbolSpec(
        file="teto_drill_rage--IMG_2118.jpg", tier="high", mode="key",
        note="фон ровно голубой (194,215,218), кейится чисто. Отдельные "
             "куски катаканы-звуковых эффектов выбрасываются (min_frac): "
             "они не связаны с телом, растягивали alpha-bbox на всю ширину "
             "кадра (персонаж выходил на треть мельче) и на 96 px "
             "читались как серый мусор вокруг символа",
        tol_lo=14, tol_hi=36, min_frac=0.20,
    ),
    "teto_chimera": SymbolSpec(
        file="teto_chimera--IMG_2116.jpg", tier="high", mode="key",
        note="фотожаба «головы Тето на теле собаки» (НЕ Цербер, см. "
             "докстринг); белый фон, но когти собаки почти белые → tol "
             "низкий, иначе заливка отъест пальцы",
        tol_lo=8, tol_hi=22,
    ),
    # ── LOW ──────────────────────────────────────────────────────────────
    "skull_cringe": SymbolSpec(
        file="skull_cringe--IMG_2113.jpg", tier="low", mode="key",
        note="скетч ручкой: бумага остаётся подложкой (иначе чёрная линия "
             "исчезнет на #0d0a18), силуэт зашивается закрытием, бумага "
             "перекрашивается дуотоном; слева срезан тёмный край скана",
        crop=(9, 2, 719, 906), tol_lo=26, tol_hi=64, seal=11,
        # бумага не чисто белая, а на пару шагов темнее (#d8d2e4): чистый
        # белый спорил бы по яркости с холодно-белым рантом HIGH-символов,
        # и LOW-символ читался бы как дорогой
        duotone=(INK, (0xD8, 0xD2, 0xE4)), sharpen=0.4,
    ),
    "utau_note": SymbolSpec(
        file="utau_note--IMG_2117.jpg", tier="low", mode="key",
        note="фигурка на почти белом (254); исходник 225x225 — самый "
             "мелкий в наборе, апскейл держим у предела MAX_UPSCALE",
        tol_lo=7, tol_hi=20, sharpen=1.0,
    ),
    "baguette_crumb": SymbolSpec(
        file="baguette_crumb--IMG_2131.jpg", tier="low", mode="key",
        note="груша с лицом Тето. Три отдельные проблемы: (1) снизу кадра "
             "тёмная полоса столешницы — срезана кропом до y=703; (2) "
             "справа-снизу плотное ядро фото-тени, оно темнее flood_tol и "
             "иначе остаётся рваным хвостом (и раздувает alpha-bbox до "
             "края кадра, из-за чего груша съезжает влево и мельчает) — "
             "снимается desat_wipe по серости, бок груши насыщенный и не "
             "страдает; (3) размытая кромка — поджимается choke, иначе "
             "остаётся белёсый кант",
        crop=(0, 0, 735, 703),
        desat_wipe=[(500, 590, 735, 703, 46, 170)],
        tol_lo=30, tol_hi=54, flood_tol=88, choke=(0.30, 0.80),
    ),
    "drill_lollipop": SymbolSpec(
        file="drill_lollipop--IMG_2132.jpg", tier="low", mode="key",
        note="яблоко с лицом Тето; вотермарка «applto» (x316..414, "
             "y39..75) замазывается белым ДО кейинга, иначе вмерзает в "
             "ассет и ломает центрирование. Плюс контактная тень под "
             "плодом (y>=330) снимается desat_wipe по серости",
        wipe=[(300, 24, 447, 92)],
        desat_wipe=[(96, 330, 447, 447, 42, 175)],
        tol_lo=26, tol_hi=50, flood_tol=84, choke=(0.30, 0.80),
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# матирование
# ─────────────────────────────────────────────────────────────────────────────
def _border_bg(rgb: np.ndarray) -> np.ndarray:
    """Цвет фона = медиана рамки кадра (а не «проверка на чистый белый»)."""
    ring = np.concatenate([
        rgb[:3].reshape(-1, 3), rgb[-3:].reshape(-1, 3),
        rgb[:, :3].reshape(-1, 3), rgb[:, -3:].reshape(-1, 3),
    ])
    return np.median(ring, axis=0)


def key_matte(rgb: np.ndarray, spec: SymbolSpec) -> tuple[np.ndarray, np.ndarray]:
    """Альфа заливкой ОТ ГРАНИЦЫ кадра + un-premultiply кромки.

    Возвращает (rgb_без_фонового_гало, alpha 0..1).
    """
    h, w, _ = rgb.shape
    bg = _border_bg(rgb)
    dist = np.abs(rgb - bg).max(axis=2)  # L∞ по каналам — хватает для плоского фона

    flood_tol = spec.flood_tol or spec.tol_hi
    lab, n = ndimage.label(dist <= flood_tol)
    border = np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])
    outside_ids = set(int(i) for i in np.unique(border) if i)
    outside = (
        np.isin(lab, list(outside_ids)) if outside_ids
        else np.zeros_like(lab, bool)
    )

    # плавная кромка только внутри заведомо-фоновой области
    ramp = np.clip((dist - spec.tol_lo) / max(spec.tol_hi - spec.tol_lo, 1e-6), 0, 1)
    alpha = np.where(outside, ramp, 1.0).astype(np.float32)

    # уровни альфы: поджим размытой фото-кромки
    lo, hi = spec.choke
    if (lo, hi) != (0.0, 1.0):
        alpha = np.clip((alpha - lo) / max(hi - lo, 1e-6), 0, 1)

    solid = alpha > 0.5
    if spec.seal:  # зашить открытый контур (скетч)
        r = spec.seal
        yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
        disk = (xx ** 2 + yy ** 2) <= r * r
        solid = ndimage.binary_closing(solid, structure=disk)
    solid = ndimage.binary_fill_holes(solid)  # страховка от дыр в субъекте

    # мусор от JPEG: выбросить крошечные изолированные компоненты
    # (+ опционально мелкие ОТДЕЛЬНЫЕ куски — см. min_frac у teto_drill_rage)
    min_area = max(24.0, 4e-5 * w * h)
    lab2, n2 = ndimage.label(solid)
    if n2:
        areas = np.asarray(
            ndimage.sum(np.ones_like(lab2), lab2, index=range(1, n2 + 1))
        )
        if spec.min_frac:
            min_area = max(min_area, spec.min_frac * float(areas.max()))
        drop = [i + 1 for i, a in enumerate(areas) if a < min_area]
        if drop:
            solid[np.isin(lab2, drop)] = False

    alpha = np.maximum(alpha, solid.astype(np.float32))
    alpha[~solid & (alpha > 0.5)] = 0.0
    if spec.min_frac:
        # у выброшенных кусков надо убить и полупрозрачную бахрому: иначе
        # призрачные контуры остаются, тянут alpha-bbox и дают дырки в альфе
        keep = ndimage.binary_dilation(solid, iterations=2)
        alpha *= keep.astype(np.float32)

    # un-premultiply: убрать плёнку фона с полупрозрачных пикселей
    out = rgb.astype(np.float32)
    band = (alpha > 0.02) & (alpha < 0.985)
    if band.any():
        a = alpha[band][:, None]
        fg = (out[band] - (1.0 - a) * bg[None, :]) / np.maximum(a, 0.02)
        out[band] = np.clip(fg, 0, 255)

    if spec.duotone:  # скетч → чернила/бумага
        ink, paper = (np.array(c, np.float32) for c in spec.duotone)
        lum = out.mean(axis=2, keepdims=True) / 255.0
        lum = np.clip((lum - 0.16) / 0.72, 0, 1) ** 0.92
        out = ink[None, None, :] + (paper - ink)[None, None, :] * lum

    return out, alpha


def _superellipse(w: int, h: int, feather: float, n: float = 4.0) -> np.ndarray:
    """Маска скруглённого квадрата (squircle) с растушёванным краем."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    ax, ay = (w - 1) / 2.0, (h - 1) / 2.0
    u = np.abs((xx - ax) / ax)
    v = np.abs((yy - ay) / ay)
    r = (u ** n + v ** n) ** (1.0 / n)  # 1.0 = граница фигуры
    # растушёвка шириной feather px вдоль минимальной полуоси
    band = feather / min(ax, ay)
    return np.clip((1.0 - r) / max(band, 1e-6), 0, 1).astype(np.float32)


def medallion_matte(rgb: np.ndarray, spec: SymbolSpec) -> tuple[np.ndarray, np.ndarray]:
    """Медальон: сохранённый фон читается как намеренная подложка."""
    h, w, _ = rgb.shape
    alpha = _superellipse(w, h, spec.feather)
    out = rgb.astype(np.float32)

    if spec.pink_lift:  # тёмный скаттер не должен утонуть в тёмном UI
        k = spec.pink_lift
        shade = 1.0 - (out.mean(axis=2, keepdims=True) / 255.0)
        tint = np.array(ACCENT_PINK, np.float32)[None, None, :]
        out = out * (1 - k * shade) + tint * (k * shade)

    if spec.vignette:  # затемнение к краю — «портрет», а не «обрезок фото»
        r = 1.0 - _superellipse(w, h, min(w, h) * 0.40)
        out *= (1.0 - spec.vignette * r[..., None])

    return np.clip(out, 0, 255), alpha


# ─────────────────────────────────────────────────────────────────────────────
# нормализация кадра
# ─────────────────────────────────────────────────────────────────────────────
def normalize(
    rgb: np.ndarray, alpha: np.ndarray, sharpen: float
) -> tuple[Image.Image, float]:
    """Вписать субъект по РЕАЛЬНОМУ alpha-bbox в WORK*FIT_RATIO, центрировать."""
    ys, xs = np.nonzero(alpha > 0.06)
    if not len(ys):
        raise RuntimeError("пустая альфа — матирование не нашло субъекта")
    t, b, l, r = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1

    sub = np.dstack([rgb, alpha * 255.0]).astype(np.uint8)[t:b, l:r]
    im = Image.fromarray(sub, "RGBA")
    sw, sh = im.size

    target = WORK * FIT_RATIO
    scale = target / max(sw, sh)
    native = scale / SS  # масштаб относительно исходных пикселей на выходе 512
    if native > MAX_UPSCALE:
        scale = MAX_UPSCALE * SS
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    if sharpen:
        im = im.filter(
            ImageFilter.UnsharpMask(
                radius=2.0, percent=int(60 * sharpen), threshold=3
            )
        )

    canvas = Image.new("RGBA", (WORK, WORK), (0, 0, 0, 0))
    canvas.paste(im, ((WORK - nw) // 2, (WORK - nh) // 2))
    return canvas, scale / SS


# ─────────────────────────────────────────────────────────────────────────────
# общая обработка
# ─────────────────────────────────────────────────────────────────────────────
def _solid(color: tuple[int, int, int], mask: np.ndarray) -> Image.Image:
    layer = Image.new("RGBA", (WORK, WORK), color + (0,))
    layer.putalpha(Image.fromarray((np.clip(mask, 0, 1) * 255).astype(np.uint8), "L"))
    return layer


def _vgrad(
    top: tuple[int, int, int], bottom: tuple[int, int, int], mask: np.ndarray
) -> Image.Image:
    ramp = np.linspace(0, 1, WORK, dtype=np.float32)[:, None, None]
    a = np.array(top, np.float32)[None, None, :]
    b = np.array(bottom, np.float32)[None, None, :]
    rgb = (a + (b - a) * ramp).repeat(WORK, axis=1)
    layer = np.dstack([rgb, np.clip(mask, 0, 1) * 255.0]).astype(np.uint8)
    return Image.fromarray(layer, "RGBA")


def _blur(mask: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return mask
    return ndimage.gaussian_filter(mask.astype(np.float32), sigma)


def treat(subject: Image.Image, tier: Tier) -> Image.Image:
    """Единая обводка-стикер + римлайт + свечение + грейдинг."""
    a = np.asarray(subject.getchannel("A"), np.float32) / 255.0

    # --- грейдинг субъекта -------------------------------------------------
    body = subject
    rgb = body.convert("RGB")
    rgb = ImageEnhance.Color(rgb).enhance(tier.saturation)
    rgb = ImageEnhance.Contrast(rgb).enhance(tier.contrast)
    rgb = ImageEnhance.Brightness(rgb).enhance(tier.brightness)
    body = Image.merge("RGBA", (*rgb.split(), subject.getchannel("A")))

    # --- единое направление света: верхний хайлайт + нижняя контактная тень -
    ramp = np.linspace(1.0, 0.0, WORK, dtype=np.float32)[:, None] ** 2.2
    body.alpha_composite(_solid((255, 255, 255), ramp * a * tier.toplight))
    ramp2 = np.linspace(0.0, 1.0, WORK, dtype=np.float32)[:, None] ** 3.0
    body.alpha_composite(_solid(INK, ramp2 * a * 0.22))

    # --- огибающая силуэта: вырубка идёт по «телу», а не по каждой волосинке
    env = (_blur(a, 7.0 * SS) > 0.5)
    env |= a > 0.995
    env = ndimage.binary_fill_holes(env)
    dist = ndimage.distance_transform_edt(~env)  # px наружу от силуэта

    def ring(px: float) -> np.ndarray:
        return np.clip(px - dist + 0.5, 0, 1)

    key_m = ring(tier.keyline)
    rim_m = ring(tier.keyline + tier.rim)
    hair_m = None
    if tier.hairline:
        if tier.hairline_inner:
            # белое кольцо ВНУТРИ ранта (сразу за кейлайном), цвет тира
            # остаётся снаружи и определяет прочтение символа издалека
            hair_m = np.clip(ring(tier.keyline + tier.hairline_px) - key_m, 0, 1)
        else:
            outer = ring(tier.keyline + tier.rim + tier.hairline_px)
            hair_m = np.clip(outer - rim_m, 0, 1)

    # --- сборка ------------------------------------------------------------
    out = Image.new("RGBA", (WORK, WORK), (0, 0, 0, 0))

    if tier.halo_radius:  # широкий ореол
        halo = _blur(rim_m, tier.halo_radius)
        out.alpha_composite(_solid(tier.glow, halo * tier.halo_alpha))
    glow = _blur(rim_m, tier.glow_radius)
    out.alpha_composite(_solid(tier.glow, glow * tier.glow_alpha))

    shadow = np.roll(_blur(rim_m, 9.0 * SS), int(5 * SS), axis=0)
    out.alpha_composite(_solid((0x06, 0x04, 0x0D), shadow * tier.shadow_alpha))

    if hair_m is not None and not tier.hairline_inner:
        out.alpha_composite(_solid(tier.hairline, hair_m))
    if tier.rim_color_bottom:
        out.alpha_composite(_vgrad(tier.rim_color, tier.rim_color_bottom, rim_m))
    else:
        out.alpha_composite(_solid(tier.rim_color, rim_m))
    if hair_m is not None and tier.hairline_inner:
        out.alpha_composite(_solid(tier.hairline, hair_m))
    out.alpha_composite(_solid(INK, key_m))
    out.alpha_composite(body)

    return out


def finish(im: Image.Image) -> Image.Image:
    small = im.resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS)
    rgb = small.convert("RGB").filter(
        ImageFilter.UnsharpMask(radius=1.2, percent=42, threshold=3)
    )
    return Image.merge("RGBA", (*rgb.split(), small.getchannel("A")))


# ─────────────────────────────────────────────────────────────────────────────
def build(symbol_id: str, spec: SymbolSpec) -> str:
    path = SRC_DIR / spec.file
    if not path.exists():
        sys.exit(
            f"нет исходника {path}\n"
            f"Ожидается каталог {SRC_DIR} с 10 JPEG "
            f"(имя = <symbol_id>--IMG_xxxx.jpg)."
        )
    src = Image.open(path).convert("RGB")
    if spec.crop:
        src = src.crop(spec.crop)
    rgb = np.asarray(src, np.float32).copy()
    bg = _border_bg(rgb)
    for l, t, r, b in spec.wipe:  # вотермарка → фоном ДО кейинга
        rgb[t:b, l:r] = bg
    for l, t, r, b, sat_max, dist_max in spec.desat_wipe:
        box = rgb[t:b, l:r]
        sat = box.max(axis=2) - box.min(axis=2)
        dist = np.abs(box - bg).max(axis=2)
        box[(sat < sat_max) & (dist < dist_max)] = bg

    if spec.mode == "key":
        rgb, alpha = key_matte(rgb, spec)
    elif spec.mode == "medallion":
        rgb, alpha = medallion_matte(rgb, spec)
    else:  # pragma: no cover
        raise ValueError(spec.mode)

    canvas, native = normalize(rgb, alpha, spec.sharpen)
    out = finish(treat(canvas, TIERS[spec.tier]))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dst = OUT_DIR / f"{symbol_id}.webp"
    try:
        out.save(dst, "WEBP", quality=88, alpha_quality=92, method=6, exact=False)
    except TypeError:  # старая Pillow без alpha_quality
        out.save(dst, "WEBP", quality=88, method=6)

    # самопроверка перечитыванием файла: дыры внутри субъекта = прозрачные
    # области, НЕ связанные с рамкой холста (мелочь <60 px — дизеринг WebP)
    kb = dst.stat().st_size / 1024
    a = np.asarray(Image.open(dst).convert("RGBA").getchannel("A"))
    lab, n = ndimage.label(a < 10)
    outer = set(int(i) for i in np.unique(
        np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])
    )) - {0}
    holes = sum(
        1 for i in range(1, n + 1)
        if i not in outer and int((lab == i).sum()) > 60
    )
    ys, xs = np.nonzero(a > 200)
    long_side = max(xs.max() - xs.min(), ys.max() - ys.min()) + 1
    pad = f"{xs.min()}/{ys.min()}/{511 - xs.max()}/{511 - ys.max()}"
    return (
        f"{symbol_id:22s} {spec.tier:13s} {spec.mode:9s} "
        f"{kb:6.1f} KB  scale={native:.2f}x  тело={100 * long_side / OUT_SIZE:4.1f}%  "
        f"поля={pad:15s} дыр={holes}"
    )


def main() -> None:
    if not SRC_DIR.is_dir():
        sys.exit(f"нет каталога исходников {SRC_DIR}")
    print(f"→ {OUT_DIR}")
    for symbol_id, spec in SYMBOLS.items():
        print("  " + build(symbol_id, spec))


if __name__ == "__main__":
    main()
