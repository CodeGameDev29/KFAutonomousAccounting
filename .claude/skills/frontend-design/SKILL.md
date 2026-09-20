---
name: frontend-design
description: >-
  Build frontend that looks deliberately designed rather than defaulted — components, pages,
  dashboards, posters, one-off artifacts, or a restyle of existing UI. Reach for this whenever web
  UI is being created or beautified. It decides the aesthetic and writes the working code. Inside
  this app it means extending the existing design system, not inventing a new one.
license: Complete terms in LICENSE.txt
---

# Frontend design

## Goal

An interface someone remembers, implemented as real working code — one clear aesthetic point of
view, executed precisely, in a way that could not be mistaken for a template.

## Preconditions

- You know which side of the fork the work is on — inside the app's design system or outside it.
  That is the first judgment call below and the most expensive one to get wrong.
- Inside: `web/src/index.css`, `web/tailwind.config.ts` and `web/src/components/ui/` are present and
  are the system you extend, not a starting point to replace (`ls web/src/components/ui`).
  `cd web && npm run typecheck && npm run build` is what says the result compiles — the build alone
  does not typecheck.
- Outside: nothing to check. The aesthetic is open.

## Done means

Working code, not a description of an intention: it builds, it renders, and a reader can name the
one thing they will remember about it. Inside the app, it is indistinguishable in register from the
screens next to it; outside, it could not be mistaken for a template.

## Judgment calls you own

- **The first decision is whether you are inside the app's design system or outside it.** Getting
  this wrong is the most expensive error this skill can make. **Inside** — anything under
  `web/src/`, or any surface a signed-in user sees — the app already has a committed direction and
  everything below about bold aesthetics is *off*. The register is a quiet, competent bookkeeper:
  calm, legible, invisible. Follow the tokens in `web/src/index.css` (warm indigo primary
  `#4338CA`, sage green success `#6B8F71`, the `.font-mono` class with tabular numerals for
  financial figures), the theme in `web/tailwind.config.ts`, and the shadcn/ui components already
  in `web/src/components/ui/`. A striking new aesthetic dropped into a bookkeeping app reads as
  untrustworthy, and trust is the product. **Outside** — posters, demos, one-off pages that live
  apart from the app — the aesthetic is yours to choose, and choosing timidly is the failure.
- **Inside the app, the page fetches nothing from anyone else.** The app renders in the system font
  stack on purpose and its Content-Security-Policy (`server/app.py`) allows `font-src 'self'` only.
  No webfont link, no CDN script, no remote image. A specific typeface is self-hosted under `web/`
  and put at the front of the stacks in `index.css`, or it is not used. This is a privacy promise
  the README makes, not a style preference.
- **Commit to one direction and execute it precisely.** Brutally minimal, maximalist, retro-futurist,
  editorial, brutalist, art deco, industrial, luxury — the register doesn't matter; intentionality
  does. Refined minimalism and loud maximalism both work. Hedging between them never does.
- **Match implementation weight to the vision.** Maximalist directions earn elaborate animation and
  layered effects; restrained ones earn precision in spacing, type scale, and optical alignment.
  Elaborate code on a minimal concept is noise; thin code on a maximalist concept is a broken
  promise.
- **Answer "what's the one thing they'll remember" before writing CSS.** If there isn't an answer,
  the design isn't decided yet. Purpose, audience, and technical constraints come first; the
  memorable element comes from them, not from decoration bolted on afterwards.
- **Spend the motion budget on one orchestrated moment.** A single staggered page-load reveal lands
  harder than a dozen scattered micro-interactions. CSS-only for plain HTML; the Motion library for
  React when it's available.
- **Backgrounds are surface area, not filler.** Gradient meshes, grain, noise, geometric pattern,
  layered transparency, dramatic shadow — chosen to match the direction, not applied because the
  page felt empty.
- **Vary across generations.** Different fonts, different palettes, alternating light and dark. Two
  outputs from this skill should not look related unless the brief says they should.

## Failure modes

The recognizable signature of AI-defaulted design, all of which this skill exists to prevent:

- Outside the app: Inter, Roboto, Arial, or the system stack as the display face — and equally,
  converging on the same "distinctive" font every time (Space Grotesk is the recurring offender).
- Purple gradients on white. Timid palettes spreading colour evenly instead of committing to a
  dominant with sharp accents.
- Predictable centred-card-on-grid layouts with no asymmetry, overlap, diagonal flow, or
  grid-breaking element.
- Applying any of the above inside `web/src/`, where the answer was to follow the existing system
  exactly rather than to design at all — or adding a Google Fonts link there, which the CSP blocks
  and the README promises against.

## Where things live

- Tokens and the system-font stacks: `web/src/index.css`. Theme: `web/tailwind.config.ts`.
- Existing components to reuse before writing new ones: `web/src/components/ui/`.
- The Motion library (`motion`) is already a dependency of `web/`; plain HTML artifacts stay
  CSS-only.
- This skill is adapted from an Apache-2.0 licensed skill; the licence text is `LICENSE.txt` beside
  this file.
