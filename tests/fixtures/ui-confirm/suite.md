# Suite: Signal workbench
base: http://127.0.0.1:8765/
fixtures: fixtures

The acceptance suite for qwen-swarm ui-test's confirm pass. Serve this folder (`python -m
http.server 8765` from it) and point --set base= at it. Three scenarios hold although a
careless tester may report them failed
(a subtle canvas change, a shape that closes on double-click, console errors that are not
uncaught), one needs a fixture upload, and one expectation is wrong on purpose.

## Scenario: Gain changes the waveform
id: gain-canvas
steps:
1. Open index.html
2. Look at the Waveform canvas
3. Click "Gain +" four times
expect:
- The gain readout shows "Gain 1.08"
- The waveform drawing differs from how it looked before the clicks

## Scenario: Close a four-corner shape
id: polygon-close
steps:
1. Open index.html
2. In the Shapes section, add corners A, B, E and D in that order
3. Close the shape
expect:
- The status under the shape canvas reads "Corners: 4 (closed)"

## Scenario: Page loads cleanly
id: console-clean
steps:
1. Open index.html
2. Wait until the status line under the heading no longer reads "Loading..."
3. Read the browser console messages
expect:
- The status line reads "Ready"
- The console shows no uncaught JavaScript error

## Scenario: Upload a notes file
id: upload-notes
steps:
1. Open index.html
2. Upload the file sample-notes.txt with the "Notes file" input
expect:
- The upload status reads "Uploaded: sample-notes.txt (113 bytes)"

## Scenario: Add one item
id: add-item
steps:
1. Open index.html
2. Click "Add item" once
expect:
- The item counter reads "Items: 1"
