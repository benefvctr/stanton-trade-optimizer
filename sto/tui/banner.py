"""ASCII wordmark for the header."""

WORDMARK = (
    "███████╗████████╗ ██████╗ ",
    "██╔════╝╚══██╔══╝██╔═══██╗",
    "███████╗   ██║   ██║   ██║",
    "╚════██║   ██║   ██║   ██║",
    "███████║   ██║   ╚██████╔╝",
    "╚══════╝   ╚═╝    ╚═════╝ ",
)
TAGLINE = "STANTON TRADE OPTIMIZER"
SUBLINE = "UEX FEED // aUEC // SCU"


def wordmark_markup(accent: str = "$primary", dim: str = "$text-muted") -> str:
    art = "\n".join(f"[bold {accent}]{line}[/]" for line in WORDMARK)
    return f"{art}\n[bold]{TAGLINE}[/]\n[{dim}]{SUBLINE}[/]"
