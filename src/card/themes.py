from dataclasses import dataclass


@dataclass
class ProjectTheme:
    name: str
    color: str
    emoji: str
    header_template: str


THEMES = {
    "green": ProjectTheme("green", "green", "🟢", "green"),
    "blue": ProjectTheme("blue", "blue", "🔵", "blue"),
    "purple": ProjectTheme("purple", "purple", "🟣", "purple"),
    "orange": ProjectTheme("orange", "orange", "🟠", "orange"),
    "red": ProjectTheme("red", "red", "🔴", "red"),
    "turquoise": ProjectTheme("turquoise", "turquoise", "🩵", "turquoise"),
}


def get_theme(color: str) -> ProjectTheme:
    return THEMES.get(color, THEMES["green"])
