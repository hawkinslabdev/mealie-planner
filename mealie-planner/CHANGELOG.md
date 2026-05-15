# Changelog

## 0.3.0

- The app is now available in English, Dutch, German, Spanish, French, and Polish
- The add-on panel is now visible to non-admin users in Home Assistant

## 0.2.5

- The recipe picker now checks Mealie for new recipes the moment you open it
- Refreshed look for the login screen
- Fix: the login screen no longer crashes after a dependency update
- Fix: the app name no longer briefly disappears after the page loads

## 0.2.4

- Security: the add-on now runs under a strict AppArmor profile, limiting what it can access on the system
- Recipes in the picker now stay up to date automatically, you'll find no need for manually refreshing recipes after adding or changing recipes in Mealie
- Fix: "Open in Mealie" now opens the correct URL when running inside Home Assistant

## 0.2.2

- Security: your Mealie URL is now checked to be a proper web address before it gets saved
- Security: PIN attempts are now compared in a way that prevents guessing by response time
- Security: bad date or ID values in requests are rejected before they reach Mealie
- Security: Alpine.js is now bundled locally — no more loading it from an external website
- Security: the add-on now sends standard browser security headers with every response
- Fix: too many requests from the same device are now correctly counted in Docker mode

## 0.2.1

- Minor changes to the look and feel
- Move the settings pane to mobile-native modal
- Fix: height of title bar now matches home assistant
- Fix: lock body on scrolling (specifically on iOS devices)
- Fix: transparency issue on day-area (mobile only) 

## 0.2.0

- Added support for multiple meals per slot.
- Fix: move api-session management to server-side

## 0.1.6

- Fix: using re-roll (sparkle) on a recipe slot no longer leaves orphaned entries in Mealie

## 0.1.5

- Fix: removing a recipe no longer causes it to reappear after refreshing the page
- Fix: scrolling on mobile no longer accidentally moves the page behind a dialog
- Fix: the "Connect to Mealie" screen no longer flashes briefly on page load
- Change: recipe action buttons (random, change, open) are now larger and easier to tap