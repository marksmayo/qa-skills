# Harbour Dental (example site)

A four-page static site for a fictional dental clinic, with accessibility barriers planted on
purpose so the WCAG Auditor example has something real to find. Don't copy its markup.

```bash
python -m http.server 8137 --bind 127.0.0.1 --directory site
python ../../scripts/run_audit.py --config audit_config.json --out <run-dir>
```

`audit_config.json` audits it at WCAG 2.2 AA at 1280x800 and 320x640, with the cookie banner
dismissed after the first engine pass, a `menu-open` state at 320 px, an `errors` state on the
booking form, and the booking flow as the critical path. Form submission stays blocked; the
errors state is client-side validation only. `site/media/clinic-tour.mp4` is deliberately not
shipped, so the video can't be played (the report sends it for review).

## Planted barriers

| Barrier | Where | SC | Found by |
|---|---|---|---|
| Tab is trapped in the day picker | book.html | 2.1.2 | keyboard probe |
| Menu toggle is a `div` with a click handler | every page, under 600 px | 2.1.1 | keyboard probe (listener check) |
| Logo image link has no alt | every page | 1.1.1, 2.4.4, 4.1.2 | axe, IBM, HTMLCS |
| Navigation links #8c9aa6 on white | every page | 1.4.3 | axe, IBM, HTMLCS |
| `nav a:focus { outline: none }` | every page | 2.4.7 | focus probe |
| Fixed call bar covers focused footer links | every page | 2.4.11 | focus probe |
| `.btn` fixed width with `overflow: hidden` | index.html, book.html | 1.4.12, 1.4.4 | text-spacing and zoom probes |
| Footer text #999 on #f4f4f4 | every page | 1.4.3 | axe, IBM, HTMLCS |
| Mobile label `for="tel"`, input `id="phone"` | book.html | 1.3.1, 4.1.2 | axe, IBM, HTMLCS |
| Full name labelled only by placeholder | book.html | 3.3.2 | IBM (review) + agent |
| Errors shown only by a red border | book.html, errors state | 1.4.1, 3.3.1 | IBM (review) + agent |
| No autocomplete on name, email, phone | book.html | 1.3.5 | agent |
| Carousel auto-advances with no pause | index.html | 2.2.2 | motion probe |
| Empty `<title>` | contact.html | 2.4.2 | axe, IBM, HTMLCS |
| `lang="de"` on an English page | services.html | 3.1.1 | language probe |
| 760 px promo banner | index.html, 320 px | 1.4.10 | reflow probe |
| Tooltip can't be dismissed or hovered | index.html | 1.4.13 | hover probe |
| 18 px carousel arrows 2 px apart | index.html | 2.5.8 | axe, IBM, target-size probe |
| Section headings are styled `div`s | services.html | 1.3.1 | IBM (review) + agent |
| Three identical "Read more" links | index.html | 2.4.4 | agent |
| Fees table scroller has no tabindex | services.html, 320 px | 2.1.1 | axe |
| Video without captions track | contact.html | 1.2.2 | axe, IBM, media probe (review) |

Things that look like problems but aren't, so the triage has something to dismiss: the fees table
scrolls inside its own container at 320 px (exempt), the "click here" map link is explained by its
sentence, the cookie banner and slides have enough contrast even though engines can't compute it,
and the video is two-dimensional content for 1.4.10.
