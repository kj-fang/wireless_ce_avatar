import re
from collections import defaultdict

def softAP_supported_channel(log_text: str) -> str:
    results = defaultdict(lambda: {
        "Supported Channels": set(),
        "Unsupported Channels": set(),
        "Pan Same Channel Only": set(),
        "Invalid Channels": set()
    })

    current_country = None

    mcc_pattern = re.compile(
        r'\[prvLarStoreCurrentMcc\].*CurrMCC:\s*(0x[0-9a-fA-F]+)\s*\((.*?)\)'
    )
    ch_pattern = re.compile(
        r'\[prvLarChannelArrUpdate\].*channel:\s*(\d+).*CHAN_FEATURE_BITS:\s*(0x[0-9a-fA-F]+)'
    )

    for line in log_text.splitlines():
        mcc_match = mcc_pattern.search(line)
        if mcc_match:
            mcc_hex = mcc_match.group(1).lower()
            country = mcc_match.group(2)

            if mcc_hex in ("0x3030", "0xa5a5"):
                current_country = None
            else:
                current_country = country
            continue

        ch_match = ch_pattern.search(line)
        if ch_match and current_country:
            ch = int(ch_match.group(1))
            value = int(ch_match.group(2), 16)

            bit3 = (value >> 3) & 1
            bit5 = (value >> 5) & 1
            bit6 = (value >> 6) & 1
            bit7 = (value >> 7) & 1

            if bit5 == 1 or bit7 == 1:
                results[current_country]["Unsupported Channels"].add(ch)

            elif bit3 == 0 and bit6 == 0:
                results[current_country]["Unsupported Channels"].add(ch)

            elif bit3 == 0 and bit6 == 1:
                results[current_country]["Pan Same Channel Only"].add(ch)

            elif bit3 == 1 and bit6 == 0:
                results[current_country]["Invalid Channels"].add(ch)

            elif bit3 == 1 and bit6 == 1:
                results[current_country]["Supported Channels"].add(ch)

    if not results:
        return "ERROR: No MCC/channel data parsed."

    output = format_output(results)

    if not output.strip():
        return "ERROR: Tool executed but produced empty result."

    return output



def format_output(results):
    lines = []

    for country in sorted(results.keys()):
        lines.append(f"{country}:")

        for category in [
            "Supported Channels",
            "Unsupported Channels",
            "Pan Same Channel Only",
            "Invalid Channels"
        ]:
            channels = sorted(results[country][category])
            ch_str = ", ".join(f"Channel {c}" for c in channels) if channels else "None"
            lines.append(f"{category}: {ch_str}")

        lines.append("")

    return "\n".join(lines)