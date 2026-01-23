def murmur2_x86(data: str, seed: int) -> int:
    "Return Murmur2 x86 hash of UTF-8 `data` with `seed`."
    m = 0x5BD1E995
    data_bytes = data.encode("utf-8")
    length = len(data_bytes)
    h = seed ^ length
    rounded_end = length & 0xFFFFFFFC
    for i in range(0, rounded_end, 4):
        k = int.from_bytes(data_bytes[i : i + 4], "little")
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> 24
        k = (k * m) & 0xFFFFFFFF

        h = (h * m) & 0xFFFFFFFF
        h ^= k

    val = length & 0x03
    k = 0
    if val >= 3: k = data_bytes[rounded_end + 2] << 16
    if val >= 2: k |= data_bytes[rounded_end + 1] << 8
    if val >= 1:
        k |= data_bytes[rounded_end]
        h ^= k
        h = (h * m) & 0xFFFFFFFF

    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15
    return h


DEBUG_HASH_SEED = 0xC70F6907
