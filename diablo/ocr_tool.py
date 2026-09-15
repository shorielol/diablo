"""
D.I.A.B.L.O. — retired.

This module used to take a full-screen pyautogui screenshot, run easyocr over
it, and click the first text that matched — with no permission check, no window
scoping, no staleness check and no verification that the click did anything.
It was never wired into the tool registry, which is the only reason it never
fired. Left as it was, it was one import away from being the hole every other
guard in Diablo exists to prevent.

Everything it tried to do now exists properly:

    reading the screen      screen_context.look / screen.capture_window
                            Apple Vision rather than easyocr, per window
                            rather than the whole desktop, so one app's text
                            never lands in another's context

    finding something       pointing.locate — measured boxes, a match score,
                            and silence rather than a guess below it

    clicking it             computer_use.click — off by default, needs an
                            explicit grant, re-checks the screen has not moved
                            since the coordinate was chosen, asks again for
                            anything destructive, and verifies afterwards that
                            the click actually changed something

The functions stay, refusing, so that anything still importing them fails
loudly and in a safe direction rather than silently doing the old thing.
"""


def capture_and_ocr():
    """Retired. Use screen_context.look() or screen.capture_window()."""
    return ("Error: this OCR path was retired. Screen reading goes through "
            "screen_context.look(), which reads one window with Apple Vision.")


def find_and_click_text(target_text: str):
    """Retired. Clicking requires a permission grant — see computer_use.click."""
    return ("Error: this click path was retired because it clicked without "
            "asking. Use pointing.locate() to find the target, then "
            "computer_use.click(), which requires explicit permission.")
