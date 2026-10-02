# TMD Chat

TMD Chat is a private company / shop / team communication platform.

## Live application

Production: https://tmd-chat.getvoroa.com
GitHub: https://github.com/pinturaj873-bit/tmd-chat

## Current application

- WhatsApp-style responsive desktop + mobile web interface
- Mobile-number OTP login foundation
- Company / Shop / Team profile
- One-to-one chat
- Group chat creation and member selection
- Company announcements / team discussion channels
- Photo, document and voice-message upload foundation
- Unread notification foundation
- Computer linking QR flow
- PWA install support for supported browsers
- Voroa health endpoint

## Important production work still required

- Connect a real SMS OTP provider; DEV_OTP is only for development/testing.
- Move SQLite and uploads to durable managed storage/database before scaling.
- Add WebSocket realtime transport and push notifications.
- Add native Android/iOS shells after the web/PWA flow is stable.
- Add production rate limiting, audit logging, device/session management and stronger access controls.

The production service is deployed on Voroa from the main branch.
