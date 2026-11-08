# iOS navigation

Open `Firesight/Firesight.xcodeproj` in Xcode on a Mac. The app targets iOS 17+.

Navigation starts on a 3D cutaway of the current floor. The blue position marker,
white heading pointer, blue route, gold destination, and stair labels follow the
current pose. Expand the map to explore or recenter it. Home ends navigation and
returns to the current-location and destination pickers.

Live walking starts automatically. Hold the phone facing the direction you walk.
Choose your initial room or scan a room QR; the app does not have an absolute indoor
GPS fix. Motion advances the position estimate, room-number recognition refines it,
and QR scans correct it. Confirm a floor change at the stairs or scan a QR on the
new floor. Demo walk is separate and returning to Live walking restores the last
live position. The iOS fire simulation has been removed.

Voice uses Apple's on-device speech synthesis, without a Fish API key. Voice on/off
controls automatic prompts; Replay speaks the current instruction again and enables
voice. Playback uses the playback audio category so the silent switch does not mute
it. Media volume and the selected audio output (including Bluetooth) still apply.

## Automated checks on macOS

From the repository root, find an available simulator and run the suite:

```sh
xcodebuild -project ios/Firesight/Firesight.xcodeproj -scheme Firesight -showdestinations
xcodebuild -project ios/Firesight/Firesight.xcodeproj -scheme Firesight -destination 'platform=iOS Simulator,id=<simulator-id>' test
```

`NavigationTests` covers acceleration in g, stationary samples, stair guidance,
demo progress across floors, real/demo isolation, heading-relative directions,
current-room labels, distant stair rejection, and returning home during route loading.

## Device verification

1. Pick a start room and destination. Confirm Start enables after routing finishes;
   unreachable destinations show an explanation.
2. Walk with motion/location permissions enabled. Check the step count, position,
   remaining distance, facing pointer, and spoken directions. Rotate 180 degrees
   to check the turn-around instruction. Deny permissions and check the visible status.
3. Expand the map, explore, then recenter. Only the current floor and its route should
   appear. Confirm a stair transition and check that the visible floor changes.
4. Test Voice on, Voice off, and Replay with the silent switch on, nonzero media
   volume, and both speaker and Bluetooth output. Switch between camera and map.
5. Run and pause a demo across floors. Return to Live walking and verify the live
   location is restored. Background/foreground the app; tracking should restart.
6. Tap Home from both navigation and the expanded map. Choose a different start and
   destination, then verify no old movement, speech, or route remains.

Xcode, iOS frameworks, simulator rendering, and physical sensor/audio checks are not
available in the Windows development environment.
