# Ack-it-fast Teams app

This folder contains a Teams manifest and its app icons. `Ack-it-fast.zip` is a sideloading package containing the manifest and both PNG files.

## Metadata and policies

The website link points to this repository; the privacy and terms links point to the short internal-use policies alongside this manifest. The repository must be public for these URLs to be accessible to reviewers.

## Calling

`supportsCalling: true` declares audio calling support in the Teams app manifest; it does not itself implement call handling. The manifest must match the Azure Bot/Teams calling configuration and deployed calling service. The calling feature is intended to place a brief call that appears as a missed call. Video is disabled in this manifest.

## Icons

- `color.png`: 192 x 192 color app icon.
- `outline.png`: 32 x 32 transparent, white outline icon.
