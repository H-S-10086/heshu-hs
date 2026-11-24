# -*- coding: utf-8 -*-
"""纯标准库二维码（QR Code）生成模块。

本模块仅使用 Python 3.12 标准库实现 ISO/IEC 18004 二维码生成，
只负责产出“模块矩阵”（list[list[bool]]），不包含任何界面代码。
调用方可用 tkinter Canvas 按矩阵逐格绘制。

对外接口：
    make_qr_matrix(text, ecc="M", border=4) -> list[list[bool]]
    get_qr_size(text, ecc="M", border=4) -> (模块数, 总边长, 版本)
    matrix_to_ascii(matrix) -> str

说明：
    - 默认采用 Byte 模式（模式指示符 0100）+ UTF-8 编码；
      当文本中存在连续 20 个以上的数字/字母数字片段时，会自动拆分为
      更省容量的 Numeric / Alphanumeric 段（仍符合 ISO/IEC 18004）；
    - 支持版本 1~10（边长 21~57 模块），按需自动选择最小版本；
    - 支持 L/M/Q/H 四种纠错等级，Reed-Solomon 基于 GF(256)，本原多项式 0x11D；
    - 自动在 8 种掩码中选择罚分最低者。
"""

import re

# ---------------------------------------------------------------------------
# 常量与数据表
# ---------------------------------------------------------------------------

# 纠错等级在格式信息中的 2 位编码：L=01, M=00, Q=11, H=10
_ECC_LEVEL_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}

# 数据编码模式（同时也是模式指示符的 4 位值）
_MODE_NUMBER = 0b0001       # 纯数字
_MODE_ALPHA_NUM = 0b0010    # 字母数字
_MODE_BYTE = 0b0100         # 字节（UTF-8）

# 字母数字模式支持的 45 个字符，下标即其编码值
_ALPHA_NUM_BYTES = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
_ALPHA_NUM_VALUE = {ch: i for i, ch in enumerate(_ALPHA_NUM_BYTES)}

# 各模式“字符计数指示符”的位数（按版本分三档）
_COUNT_BITS_SMALL = {_MODE_NUMBER: 10, _MODE_ALPHA_NUM: 9, _MODE_BYTE: 8}
_COUNT_BITS_MEDIUM = {_MODE_NUMBER: 12, _MODE_ALPHA_NUM: 11, _MODE_BYTE: 16}
_COUNT_BITS_LARGE = {_MODE_NUMBER: 14, _MODE_ALPHA_NUM: 13, _MODE_BYTE: 16}

# 分段优化的最小连续长度：只有连续 20 个以上的数字/字母数字才值得拆分
_SEGMENT_MIN = 20

# 支持的最大版本（本模块按项目需要实现到版本 10）
_MAX_VERSION = 10

# Reed-Solomon 分块表。
# 结构：版本 -> 纠错等级 -> (每块纠错码字数, [(块数, 每块数据码字数), ...])
# 数据来源：ISO/IEC 18004 表 9（与 python-qrcode 的 RS_BLOCK_TABLE 一致）。
_RS_BLOCK_TABLE = {
    1: {
        "L": (7, [(1, 19)]),
        "M": (10, [(1, 16)]),
        "Q": (13, [(1, 13)]),
        "H": (17, [(1, 9)]),
    },
    2: {
        "L": (10, [(1, 34)]),
        "M": (16, [(1, 28)]),
        "Q": (22, [(1, 22)]),
        "H": (28, [(1, 16)]),
    },
    3: {
        "L": (15, [(1, 55)]),
        "M": (26, [(1, 44)]),
        "Q": (18, [(2, 17)]),
        "H": (22, [(2, 13)]),
    },
    4: {
        "L": (20, [(1, 80)]),
        "M": (18, [(2, 32)]),
        "Q": (26, [(2, 24)]),
        "H": (16, [(4, 9)]),
    },
    5: {
        "L": (26, [(1, 108)]),
        "M": (24, [(2, 43)]),
        "Q": (18, [(2, 15), (2, 16)]),
        "H": (22, [(2, 11), (2, 12)]),
    },
    6: {
        "L": (18, [(2, 68)]),
        "M": (16, [(4, 27)]),
        "Q": (24, [(4, 19)]),
        "H": (28, [(4, 15)]),
    },
    7: {
        "L": (20, [(2, 78)]),
        "M": (18, [(4, 31)]),
        "Q": (18, [(2, 14), (4, 15)]),
        "H": (26, [(4, 13), (1, 14)]),
    },
    8: {
        "L": (24, [(2, 97)]),
        "M": (22, [(2, 38), (2, 39)]),
        "Q": (22, [(4, 18), (2, 19)]),
        "H": (26, [(4, 14), (2, 15)]),
    },
    9: {
        "L": (30, [(2, 116)]),
        "M": (22, [(3, 36), (2, 37)]),
        "Q": (20, [(4, 16), (4, 17)]),
        "H": (24, [(4, 12), (4, 13)]),
    },
    10: {
        "L": (18, [(2, 68), (2, 69)]),
        "M": (26, [(4, 43), (1, 44)]),
        "Q": (24, [(6, 19), (2, 20)]),
        "H": (28, [(6, 15), (2, 16)]),
    },
}

# 各版本校正图案的中心坐标（行列共用同一套坐标）。
_ALIGNMENT_POSITIONS = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}

# GF(256) 指数/对数表，本原多项式 0x11D（x^8 + x^4 + x^3 + x^2 + 1）
_GF_EXP = [0] * 512
_GF_LOG = [0] * 256


def _init_gf_tables():
    """在导入时构建 GF(256) 的指数表与对数表（纯计算，无副作用）。"""
    value = 1
    for i in range(255):
        _GF_EXP[i] = value
        _GF_LOG[value] = i
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]


_init_gf_tables()


def _gf_mul(a, b):
    """GF(256) 乘法。"""
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


# 生成多项式缓存，避免重复计算
_GENERATOR_CACHE = {}


def _rs_generator(degree):
    """返回 degree 次 Reed-Solomon 生成多项式（系数按高次在前）。

    生成多项式为 (x - α^0)(x - α^1)...(x - α^(degree-1))。
    """
    cached = _GENERATOR_CACHE.get(degree)
    if cached is not None:
        return cached
    poly = [1]
    for i in range(degree):
        # 当前多项式乘以 (x - α^i)
        nxt = [0] * (len(poly) + 1)
        root = _GF_EXP[i]
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, root)
        poly = nxt
    _GENERATOR_CACHE[degree] = poly
    return poly


def _rs_encode(data, ec_count):
    """对一段数据码字计算 ec_count 个纠错码字。"""
    gen = _rs_generator(ec_count)
    buf = list(data) + [0] * ec_count
    for i in range(len(data)):
        coef = buf[i]
        if coef:
            # 用生成多项式消去当前最高次项
            for j in range(1, len(gen)):
                buf[i + j] ^= _gf_mul(gen[j], coef)
    return buf[len(data):]


# ---------------------------------------------------------------------------
# 容量与版本选择
# ---------------------------------------------------------------------------

def _data_codewords(version, ecc):
    """返回指定版本、纠错等级下的数据码字总数。"""
    _, groups = _RS_BLOCK_TABLE[version][ecc]
    return sum(count * data_len for count, data_len in groups)


def _max_data_bytes(version, ecc):
    """返回该版本/等级下“纯 Byte 模式”可容纳的最大字节数（用于提示）。"""
    cc_bits = 8 if version <= 9 else 16
    return (_data_codewords(version, ecc) * 8 - 4 - cc_bits) // 8


def _count_bits(mode, version):
    """返回指定模式、指定版本下字符计数指示符的位数。"""
    if version <= 9:
        return _COUNT_BITS_SMALL[mode]
    if version <= 26:
        return _COUNT_BITS_MEDIUM[mode]
    return _COUNT_BITS_LARGE[mode]


def _segments_bits(segments, version):
    """计算整组数据段在该版本下的总位数（含模式指示符与字符计数）。"""
    return sum(
        4 + _count_bits(mode, version) + _segment_data_bits(mode, chunk)
        for mode, chunk in segments
    )


def _pick_version(segments, text_len, ecc):
    """从版本 1 开始向上查找能容纳全部数据段的最小版本。"""
    for version in range(1, _MAX_VERSION + 1):
        if _segments_bits(segments, version) <= _data_codewords(version, ecc) * 8:
            return version
    limit = _max_data_bytes(_MAX_VERSION, ecc)
    raise ValueError(
        "文本过长，无法生成二维码：纠错等级 %s 时本模块最多支持约 %d 字节"
        "（版本 %d，UTF-8 编码），当前文本为 %d 字节。"
        "请缩短内容或改用更低的纠错等级。"
        % (ecc, limit, _MAX_VERSION, text_len)
    )


# ---------------------------------------------------------------------------
# 数据编码
# ---------------------------------------------------------------------------

def _regex_split(raw, pattern):
    """按正则 pattern 把 raw 切成交替的 (是否命中, 片段) 序列。"""
    while raw:
        match = pattern.search(raw)
        if not match:
            break
        start, end = match.start(), match.end()
        if start:
            yield False, raw[:start]
        yield True, raw[start:end]
        raw = raw[end:]
    if raw:
        yield False, raw


def _split_segments(raw):
    """把 UTF-8 字节串切分成 [(模式, 字节片段), ...]。

    先按连续数字切分，再把剩下的片段按连续字母数字切分；
    只有连续 20 个及以上才拆分，避免短片段因模式切换反而变长。
    """
    if not raw:
        return []
    if len(raw) <= _SEGMENT_MIN:
        num_pattern = re.compile(rb"^\d+$")
        alpha_pattern = re.compile(rb"^[" + re.escape(_ALPHA_NUM_BYTES) + rb"]+$")
    else:
        num_pattern = re.compile(rb"\d{%d,}" % _SEGMENT_MIN)
        alpha_pattern = re.compile(
            b"[" + re.escape(_ALPHA_NUM_BYTES) + b"]{%d,}" % _SEGMENT_MIN
        )

    segments = []
    for is_number, chunk in _regex_split(raw, num_pattern):
        if is_number:
            segments.append((_MODE_NUMBER, chunk))
            continue
        for is_alpha, sub in _regex_split(chunk, alpha_pattern):
            mode = _MODE_ALPHA_NUM if is_alpha else _MODE_BYTE
            segments.append((mode, sub))
    return segments


def _segment_data_bits(mode, chunk):
    """计算单个数据段的内容位数（不含模式指示符与字符计数）。"""
    if mode == _MODE_NUMBER:
        groups, remainder = divmod(len(chunk), 3)
        return groups * 10 + (0, 4, 7)[remainder]
    if mode == _MODE_ALPHA_NUM:
        pairs, remainder = divmod(len(chunk), 2)
        return pairs * 11 + remainder * 6
    return len(chunk) * 8


def _encode_codewords(segments, version, ecc):
    """把数据段编码成数据码字序列（含终止符与填充字节）。"""
    bits = []

    def push(value, length):
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    for mode, chunk in segments:
        push(mode, 4)                          # 模式指示符
        push(len(chunk), _count_bits(mode, version))  # 字符计数
        if mode == _MODE_NUMBER:
            # 每 3 位数字压成 10 位，余 2 位压 7 位、余 1 位压 4 位
            for i in range(0, len(chunk), 3):
                part = chunk[i:i + 3]
                push(int(part), (0, 4, 7, 10)[len(part)])
        elif mode == _MODE_ALPHA_NUM:
            # 每 2 个字符压成 11 位，落单的字符压 6 位
            for i in range(0, len(chunk), 2):
                part = chunk[i:i + 2]
                if len(part) == 2:
                    push(_ALPHA_NUM_VALUE[part[0]] * 45 + _ALPHA_NUM_VALUE[part[1]], 11)
                else:
                    push(_ALPHA_NUM_VALUE[part[0]], 6)
        else:
            for byte in chunk:
                push(byte, 8)                  # 每字节 8 位

    capacity_bits = _data_codewords(version, ecc) * 8
    # 终止符：最多 4 个 0，容量不足时截断
    for _ in range(min(4, capacity_bits - len(bits))):
        bits.append(0)
    # 补 0 直到按字节对齐
    while len(bits) % 8:
        bits.append(0)
    # 组装为码字
    codewords = []
    for i in range(0, len(bits), 8):
        byte = 0
        for bit in bits[i:i + 8]:
            byte = (byte << 1) | bit
        codewords.append(byte)
    # 用 0xEC / 0x11 交替填充剩余容量
    pad = (0xEC, 0x11)
    index = 0
    while len(codewords) < capacity_bits // 8:
        codewords.append(pad[index & 1])
        index += 1
    return codewords


def _interleave(codewords, version, ecc):
    """按分块结构计算纠错码字，并按规范交织数据块与纠错块。"""
    ec_count, groups = _RS_BLOCK_TABLE[version][ecc]
    blocks = []
    pos = 0
    for count, data_len in groups:
        for _ in range(count):
            blocks.append(codewords[pos:pos + data_len])
            pos += data_len
    ec_blocks = [_rs_encode(block, ec_count) for block in blocks]

    result = []
    # 先交织数据码字（短块跳过已越界的下标）
    max_len = max(len(block) for block in blocks)
    for i in range(max_len):
        for block in blocks:
            if i < len(block):
                result.append(block[i])
    # 再交织纠错码字
    for i in range(ec_count):
        for ec in ec_blocks:
            result.append(ec[i])
    return result


# ---------------------------------------------------------------------------
# 矩阵布局
# ---------------------------------------------------------------------------

def _create_base_matrix(version):
    """构建带有全部功能图案的矩阵。

    返回 (modules, is_function)：
        modules     当前模块颜色（bool，先按白色初始化）
        is_function 标记该位置是否属于功能图案（不可被数据占用）
    """
    size = version * 4 + 17
    modules = [[False] * size for _ in range(size)]
    is_func = [[False] * size for _ in range(size)]

    def put(row, col, dark):
        modules[row][col] = dark
        is_func[row][col] = True

    # 1) 三个定位图案（7x7）及其分隔符（四周一圈白）
    for base_row, base_col in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                row, col = base_row + dr, base_col + dc
                if 0 <= row < size and 0 <= col < size:
                    if 0 <= dr <= 6 and 0 <= dc <= 6:
                        dark = (
                            dr in (0, 6)
                            or dc in (0, 6)
                            or (2 <= dr <= 4 and 2 <= dc <= 4)
                        )
                    else:
                        dark = False  # 分隔符为白色
                    put(row, col, dark)

    # 2) 时序图案（第 6 行与第 6 列，偶数下标为黑；不覆盖已存在的功能模块）
    for i in range(size):
        if not is_func[6][i]:
            put(6, i, i % 2 == 0)
        if not is_func[i][6]:
            put(i, 6, i % 2 == 0)

    # 3) 校正图案（5x5），跳过与定位图案重叠的三个角
    positions = _ALIGNMENT_POSITIONS[version]
    last = positions[-1] if positions else -1
    for row in positions:
        for col in positions:
            if (row == 6 and col == 6) or (row == 6 and col == last) or (row == last and col == 6):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    dark = max(abs(dr), abs(dc)) != 1
                    put(row + dr, col + dc, dark)

    # 4) 固定的黑模块（暗模块），坐标 (4*version+9, 8)
    put(size - 8, 8, True)

    # 5) 预留格式信息区域（值稍后写入，这里只占位）
    for i in range(9):
        is_func[8][i] = True
        is_func[i][8] = True
    for i in range(8):
        is_func[8][size - 1 - i] = True
    for i in range(7):
        is_func[size - 1 - i][8] = True

    # 6) 预留版本信息区域（仅版本 >= 7）
    if version >= 7:
        for i in range(18):
            is_func[i // 3][i % 3 + size - 11] = True
            is_func[i % 3 + size - 11][i // 3] = True

    return modules, is_func


def _map_data(modules, is_func, codewords, mask_index):
    """按“从右下角开始、每两列一次、蛇形上下”的规则放置数据位。"""
    size = len(modules)
    mask = _MASK_FUNCTIONS[mask_index]
    bit_index = 7
    byte_index = 0
    row = size - 1
    direction = -1  # -1 向上，+1 向下
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1  # 跳过时序图案所在的第 6 列
        for _ in range(size):
            for c in (col, col - 1):
                if not is_func[row][c]:
                    dark = False
                    if byte_index < len(codewords):
                        dark = ((codewords[byte_index] >> bit_index) & 1) == 1
                    if mask(row, c):
                        dark = not dark
                    modules[row][c] = dark
                    bit_index -= 1
                    if bit_index < 0:
                        bit_index = 7
                        byte_index += 1
            row += direction
            if row < 0 or row >= size:
                row -= direction
                direction = -direction
                break
        col -= 2


def _bch_format(data):
    """BCH(15,5) 编码格式信息，并异或掩码 0x5412。"""
    remainder = data << 10
    while remainder.bit_length() - 1 >= 10:
        remainder ^= 0x537 << (remainder.bit_length() - 11)
    return ((data << 10) | remainder) ^ 0x5412


def _bch_version(version):
    """BCH(18,6) 编码版本信息。"""
    remainder = version << 12
    while remainder.bit_length() - 1 >= 12:
        remainder ^= 0x1F25 << (remainder.bit_length() - 13)
    return (version << 12) | remainder


def _write_format(modules, size, ecc, mask_index):
    """写入两份冗余的格式信息（15 位，低位先放）。"""
    bits = _bch_format((_ECC_LEVEL_BITS[ecc] << 3) | mask_index)
    # 第一份：左上角竖排 + 左下角竖排
    for i in range(15):
        dark = ((bits >> i) & 1) == 1
        if i < 6:
            modules[i][8] = dark
        elif i < 8:
            modules[i + 1][8] = dark
        else:
            modules[size - 15 + i][8] = dark
    # 第二份：右上角横排 + 左上角横排
    for i in range(15):
        dark = ((bits >> i) & 1) == 1
        if i < 8:
            modules[8][size - 1 - i] = dark
        elif i == 8:
            modules[8][7] = dark
        else:
            modules[8][15 - i - 1] = dark


def _write_version(modules, size, version):
    """写入版本信息（18 位），仅版本 >= 7 需要。"""
    bits = _bch_version(version)
    for i in range(18):
        dark = ((bits >> i) & 1) == 1
        modules[i // 3][i % 3 + size - 11] = dark
    for i in range(18):
        dark = ((bits >> i) & 1) == 1
        modules[i % 3 + size - 11][i // 3] = dark


# ---------------------------------------------------------------------------
# 掩码与罚分
# ---------------------------------------------------------------------------

_MASK_FUNCTIONS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r * c) % 3 + (r + c) % 2) % 2 == 0,
)

# 规则 3 需要识别的两个 11 位图案
_PENALTY_PATTERN_A = (
    True, False, True, True, True, False, True, False, False, False, False,
)
_PENALTY_PATTERN_B = (
    False, False, False, False, True, False, True, True, True, False, True,
)


def _penalty(modules):
    """按 ISO/IEC 18004 的四条规则计算掩码罚分。"""
    size = len(modules)
    score = 0

    # 规则 1：行/列中连续 5 个及以上同色模块，每段罚 3 + (长度 - 5)
    for line in list(modules) + [list(column) for column in zip(*modules)]:
        run_color = line[0]
        run_len = 1
        for value in line[1:]:
            if value == run_color:
                run_len += 1
            else:
                if run_len >= 5:
                    score += run_len - 2
                run_color = value
                run_len = 1
        if run_len >= 5:
            score += run_len - 2

    # 规则 2：2x2 同色方块，每块罚 3
    for row in range(size - 1):
        upper = modules[row]
        lower = modules[row + 1]
        for col in range(size - 1):
            count = (
                int(upper[col]) + int(upper[col + 1])
                + int(lower[col]) + int(lower[col + 1])
            )
            if count == 0 or count == 4:
                score += 3

    # 规则 3：出现 1:1:3:1:1 且一侧有 4 个浅色模块的图案，每次罚 40
    for row in range(size):
        line = modules[row]
        for col in range(size - 10):
            window = tuple(line[col:col + 11])
            if window == _PENALTY_PATTERN_A or window == _PENALTY_PATTERN_B:
                score += 40
    for col in range(size):
        for row in range(size - 10):
            window = tuple(modules[row + k][col] for k in range(11))
            if window == _PENALTY_PATTERN_A or window == _PENALTY_PATTERN_B:
                score += 40

    # 规则 4：黑色模块占比偏离 50% 的程度，每偏离 5% 罚 10 分
    dark = sum(1 for row in modules for value in row if value)
    total = size * size
    percent = dark / total * 100
    score += int(abs(percent - 50) / 5) * 10
    return score


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def _normalize_ecc(ecc):
    """校验并规范化纠错等级。"""
    if not isinstance(ecc, str):
        raise ValueError("纠错等级必须是字符串，取值为 'L'、'M'、'Q'、'H' 之一。")
    key = ecc.upper()
    if key not in _ECC_LEVEL_BITS:
        raise ValueError(
            "不支持的纠错等级 '%s'，请使用 'L'、'M'、'Q'、'H' 之一。" % ecc
        )
    return key


def _normalize_border(border):
    """校验静默区宽度。"""
    if not isinstance(border, int) or isinstance(border, bool):
        raise ValueError("静默区宽度 border 必须是整数，单位为模块数（规范要求至少 4）。")
    if border < 0:
        raise ValueError("静默区宽度 border 不能为负数。")
    return border


def _build_unpadded_matrix(segments, ecc, version):
    """生成不含静默区的矩阵，并自动选出罚分最低的掩码。"""
    data_codewords = _encode_codewords(segments, version, ecc)
    all_codewords = _interleave(data_codewords, version, ecc)
    base, is_func = _create_base_matrix(version)
    size = len(base)

    best_score = None
    best_mask = 0
    for mask_index in range(8):
        trial = [row[:] for row in base]
        _map_data(trial, is_func, all_codewords, mask_index)
        # 评分时不写入格式信息/版本信息，且把固定暗模块视作白色，
        # 与主流实现（python-qrcode / zxing）的试算方式保持一致。
        trial[size - 8][8] = False
        score = _penalty(trial)
        # 严格小于才替换，保证罚分相同时取编号更小的掩码
        if best_score is None or score < best_score:
            best_score = score
            best_mask = mask_index

    result = [row[:] for row in base]
    _map_data(result, is_func, all_codewords, best_mask)
    _write_format(result, size, ecc, best_mask)
    if version >= 7:
        _write_version(result, size, version)
    return result


def make_qr_matrix(text, ecc="M", border=4):
    """生成二维码矩阵。

    参数
      text:   要编码的文本（支持任意 UTF-8 文本，本项目场景为 URL）
      ecc:    纠错等级，"L"/"M"/"Q"/"H" 四选一，默认 "M"
      border: 静默区宽度（模块数），默认 4，规范要求至少 4

    返回
      list[list[bool]]，True 表示黑色模块，False 表示白色；
      边长 = (21 + 4*(version-1)) + border*2

    异常
      ValueError：纠错等级非法、border 非法，或文本超出最大容量。
    """
    if not isinstance(text, str):
        raise TypeError("text 必须是 str 类型。")
    ecc = _normalize_ecc(ecc)
    border = _normalize_border(border)

    raw = text.encode("utf-8")
    segments = _split_segments(raw)
    version = _pick_version(segments, len(raw), ecc)
    matrix = _build_unpadded_matrix(segments, ecc, version)

    if border == 0:
        return matrix

    size = len(matrix)
    total = size + border * 2
    result = [[False] * total for _ in range(total)]
    for row in range(size):
        result[row + border][border:border + size] = matrix[row]
    return result


def get_qr_size(text, ecc="M", border=4):
    """返回 (模块数不含静默区, 总边长含静默区, version)。"""
    if not isinstance(text, str):
        raise TypeError("text 必须是 str 类型。")
    ecc = _normalize_ecc(ecc)
    border = _normalize_border(border)

    raw = text.encode("utf-8")
    segments = _split_segments(raw)
    version = _pick_version(segments, len(raw), ecc)
    modules = 21 + 4 * (version - 1)
    return modules, modules + border * 2, version


def matrix_to_ascii(matrix):
    """把矩阵转成 ASCII 字符画（'██' 表示黑，'  ' 表示白），用于调试自检。"""
    if not matrix:
        return ""
    return "\n".join(
        "".join("██" if value else "  " for value in row) for row in matrix
    )
