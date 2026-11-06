"""Wording for celebration emails.

``pick`` chooses a line from the employee and the year, so one person sees a new
line every year and nobody needs a table to remember what was sent. People who
celebrate on the same day are given different lines through ``used``.
"""

from __future__ import annotations

BIRTHDAY_OPENERS = [
    "Today is all about you.",
    "Today the whole team is celebrating you.",
    "A very special day deserves a very special wish.",
    "We are so glad to celebrate this day with you.",
    "Another wonderful year begins for you today.",
    "Today we pause to celebrate someone who matters to all of us.",
    "It is your day, and we could not be happier for you.",
]

BIRTHDAY_WISHES = [
    "May this year bring you the courage to chase what you dream of, and the joy of catching it.",
    "May every new day this year open a door you have been waiting for.",
    "Your best chapters are still unwritten. May this year be the one you are proudest of.",
    "May your hard work return to you as success, and your kindness return as happiness.",
    "Keep believing in yourself the way the people around you already do.",
    "May you grow stronger with every challenge and wiser with every success this year.",
    "May this new year of your life be filled with good health, peace and big wins.",
    "Great things are built one good day at a time. May you have many of them this year.",
    "May your smile stay bright, your goals stay clear and your confidence keep growing.",
    "May you find as much joy in the journey this year as in reaching the destination.",
    "The energy you bring lifts everyone around you. May life return it to you many times over.",
    "May this year give you new skills, new friends and new reasons to be proud.",
    "Dream a little bigger this year. You have already shown how far you can go.",
    "May every effort you make this year move you closer to the life you want.",
    "May you always have the strength to begin again and the patience to see things through.",
    "Age is only a number. What counts is how much you still want to learn, and you inspire us.",
    "May your home be full of laughter and your days be full of purpose.",
    "May this year surprise you with opportunities bigger than the ones you planned for.",
    "You make difficult days easier for the people around you. May your own path be just as bright.",
    "May you always find a reason to smile, and always be someone else's reason too.",
    "Every year adds something to who you are. May this one add confidence, calm and joy.",
    "May the goals that feel far away today feel close by your next birthday.",
    "Keep doing what you do best: showing up, trying hard and making a difference.",
    "May this be the year your quiet hard work gets the recognition it deserves.",
]

BIRTHDAY_CLOSERS = [
    "Thank you for being such a valued part of our team.",
    "We are lucky to have you with us.",
    "Enjoy every moment of your day.",
    "Have a wonderful celebration with the people you love.",
    "Take a moment today to be proud of how far you have come.",
    "Here's to the year ahead, and to everything you will achieve in it.",
]

ANNIVERSARY_WISHES = [
    "May your bond grow stronger and your home stay full of happiness with every passing year.",
    "Wishing you both a lifetime of love, trust and laughter.",
    "May the years ahead bring you even more reasons to celebrate together.",
    "May your journey together always be filled with understanding, care and joy.",
    "Wishing you many more years of shared dreams and beautiful memories.",
    "May your love continue to be a source of strength for each other, every single day.",
    "Here's to another year of togetherness, and many more happy years to come.",
]

WORK_ANNIVERSARY_WISHES = [
    "Your dedication has made a real difference, and we are grateful for it.",
    "Every year you have spent with us has made this team stronger.",
    "Thank you for your commitment, your effort and everything you bring to work each day.",
    "Your journey with us inspires the people who work beside you.",
    "We are proud of what you have achieved here, and excited for what comes next.",
    "Thank you for growing with us. The best is still ahead.",
    "Your hard work and loyalty mean a great deal to all of us.",
]


def pick(pool: list[str], employee_id: int, year: int, used: set[str] | None = None) -> str:
    """A line for this person and year that differs from last year and from ``used``."""
    # The step of 5 shares no factor with any pool size here, so a person only repeats
    # a line after the whole pool has gone round.
    index = (employee_id * 7 + year * 5) % len(pool)
    if used is not None:
        for _ in range(len(pool)):
            if pool[index] not in used:
                break
            index = (index + 1) % len(pool)
        used.add(pool[index])
    return pool[index]
