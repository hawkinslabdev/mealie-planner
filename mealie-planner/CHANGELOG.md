# Changelog

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