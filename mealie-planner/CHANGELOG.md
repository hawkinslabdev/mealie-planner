# Changelog

## 0.6.4

- Fix: Hide the version number in the settings UI on mobile

## 0.6.3

- The Mealie connection URL and token now collapse behind an "Edit" toggle once connected (#13)
- The app version now shows next to "Report an Issue" in settings
- Fix: a failed connection check (e.g. a 502 from Mealie) no longer showed the "connected" badge as if nothing were wrong (#20)
- Fix: dragging a meal to a new slot on mobile could select text instead of moving it (#21)
- Fix: configuring the connection from the settings UI could leave the mobile view stuck on its loading skeleton

## 0.6.2

- Fix: the meal plan is now kept ready in the background, so switching weeks feels instant
- Fix: a meal added or removed could briefly reappear or vanish again while the plan refreshed
- Fix: changing your Mealie connection could still show meals from the old server for a minute
- Fix: recipes without a serving size no longer show a lonely "persons" with no number
- Fix: browsing far back and forth through weeks no longer grows memory use over time
- Fix: a fresh install no longer logs a scary error before Mealie is configured

## 0.6.1

- Fix: picking a random recipe is now genuinely random and no longer waits on Mealie
- Fix: the app no longer loads fonts from the internet, so it starts instantly and works fully offline
- Fix: your meal plan now appears right away instead of waiting for the connection check to Mealie
- Fix: the app requested everything twice on startup
- Fix: on mobile, recipe names sat too high next to their thumbnail

## 0.6.0

- Recipe ingredients and steps are now visible, allowing a quick glance at your recipe without leaving the app
- Tick off ingredients and steps as you go, they're remembered until you open a different recipe
- Your screen can be kept awake while a recipe is open, so it won't dim halfway through cooking
- Recipes now have an "Open in Mealie" button at the bottom, for when you need the full page
- On mobile, changing a recipe moved into the ··· menu to keep the row uncluttered
- The connection status only appears when Mealie can't be reached, and tapping it takes you straight to settings

## 0.5.2

- Recipe images are now cached locally, you may see a slight increase in loading times
- Fix: the remove button on recipe cards (on desktop) no longer jumps away when you try to click it
- Fix: quickly toggling settings no longer sends a burst of http requests
- Fix: rapidly picking random recipes (sparkle) no longer overwhelms Mealie with request

## 0.5.1

- Add swipe gestures for quick add and settings modal
- Fix: meal import type descriptions have been shortened
- Fix: numeric keypad layout for pin code is now enforced (standalone mode only)
- Fix: use pip-tools for package management instead of pinning manually

## 0.5.0

- Add support for AI providers! Use your configured Mealie AI providers natively
- Add built-in proxy that can be used optionally when Mealie is restricted from accessing a recipe during the import process
- Add image recognition support for instances that have setup a connection to an OpenAI-compatible API (#10)
- Added Swedish, Danish, Norwegian, Brazilian Portuguese, Czechia and Russian using DeepL translations (#8)
- Refactored the application to use a proper monolithic structure (#7)
- Fix: most recently added recipe will show in the meal picker (#6)
- Fix: change week view pagination button hint from day to week (#5)
- Fix: handle orphaned recipe when recipe is deleted in Mealie
- Fix: handle unique constraint errors on recipe.slug

## 0.4.3

- Added color palette picker with 5 accent themes (amber, lavender, sage, terracotta, slate)
- Fix: "Create recipe" in quick-add no longer auto-adds to the meal plan — now consistent with "Import from URL"
- Fix: modal header title and close button now vertically centred
- Fix: various accessibility improvements (keyboard navigation, screen reader labels, focus trapping)

## 0.4.2

- Added Italian translation (thanks to @albanobattistella!) 
- Refactored locale selector in the UI for easier locale management

## 0.4.1

- Fix: remove add recipe text, replaced by the quick add button
- Fix: rewrite various translations
- Fix: change the alignment of delete confirmation toast
- Fix: change the start position of mobile timeline to current day

## 0.4.0

- You can create or import a recipe from a URL directly from the planner, without opening Mealie
- Recipe cards now show a colour gradient when no thumbnail is available
- Various settings are now stored server-side and follow you across browsers and devices
- Fix: Modal and settings overlays now animate smoothly
- Security: We've updated various packages the app relies on