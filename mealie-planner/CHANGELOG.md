# Changelog

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