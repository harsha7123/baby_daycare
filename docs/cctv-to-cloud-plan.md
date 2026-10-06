# CCTV → cloud link (final phase)

Designed by the Video Streaming, IoT Fleet and Network Engineer agents (from `.claude/agents/`), checked against
MediaMTX source in `references/01-video-ingest/mediamtx`. Prices are rough and need fresh quotes.

## 1. Edge box (one per centre)
- **Default:** Intel N100 mini-PC, 2× Ethernet, 16 GB RAM, 512 GB NVMe + 2 TB SSD, 12 V mini-UPS (~₹28–33k).
  Budget: Raspberry Pi 5 + 1 TB NVMe (~₹17k), fine for ≤ 8 cameras.
- Ubuntu Server 24.04, `/data` encrypted (LUKS + TPM), chrony NTP, Docker Compose.
- Runs: MediaMTX (pinned), `vdb-edge-agent` (enrolment, ONVIF discovery, heartbeat, updates, clip fetch),
  Grafana Alloy (metrics), Tailscale (management only).
- Cameras on the box's second NIC / VLAN with no internet route.

## 2. Transport
- **Video: SRT**, edge dials out to EC2 (ap-south-1). Works through CGNAT with no site config, fixed latency
  window, built-in AES encryption. RTSP-in-WireGuard stalls on lossy uplinks; WebRTC needs TURN and is viewer-oriented.
- Fallback where UDP is blocked: forward over `rtsps://…:8322`.
- **Substream to cloud for AI** (H.264 Main, 8 fps, GOP 16, smart-codec OFF). **Main stream stays on site**,
  recorded locally, fetched only for alert HD clips.
- Keep the cloud feed H.264: OpenCV's bundled FFmpeg decodes on CPU only. Move to GPU decode (PyNvVideoCodec)
  beyond ~6 sites per GPU.

## 3. MediaMTX
Edge `mediamtx.yml` (written by the agent, mode 0600):
```yaml
rtspTransports: [tcp]
rtmp: false
hls: false
webrtc: false
srt: false
api: true
metrics: true
playback: true
authInternalUsers:
  - user: any
    ips: [127.0.0.1/32, 100.64.0.0/10]
    permissions: [{action: read}, {action: playback}, {action: api}, {action: metrics}]
pathDefaults:
  rtspTransport: tcp
  record: true
  recordPath: /data/rec/%path/%Y-%m-%d_%H-%M-%S-%f
  recordSegmentDuration: 10m
paths:
  cam01-main:
    source: rtsp://vdb:PW@192.168.1.64:554/Streaming/Channels/101
    recordDeleteAfter: 48h
  cam01-sub:
    source: rtsp://vdb:PW@192.168.1.64:554/Streaming/Channels/102
    recordDeleteAfter: 168h
    forward:
      - dest: "srt://ingest.<domain>:8890?streamid=publish:blr01/cam01:blr01:SITE_TOKEN&passphrase=SITE_PASS&pbkeylen=32&latency=1000"
```
Cloud: add a `mediamtx` service (only `8890/udp` public), `srt: true`, `authMethod: http` pointing at a new
`/internal/mediamtx-auth` API endpoint (publish: srt + valid site token + `site/` path prefix; read: rtsp + worker creds).
Worker camera `source: rtsp://mediamtx:8554/blr01/cam01`, credentials from `VDB_MEDIA_USER` / `VDB_MEDIA_PASS`.

Worker changes still to make (`vdb/sources.py`): stream-PTS timestamps anchored to wall clock (reset on > 2 s drift);
reconnect count + frame age in perf logs. (Done already: TCP + low-delay FFmpeg options, 5 s open/read timeouts,
5 s max backoff, `env:` camera URLs.)

## 4. Bandwidth, buffering, backfill
| Cameras | D1 substream | 720p substream |
|---|---|---|
| 4 | 2.4 Mbps | 4.6 Mbps |
| 8 | 4.8 Mbps | 9.2 Mbps |
| 16 | 9.6 Mbps | 18.4 Mbps |

Keep total ≤ 50% of measured uplink; stream only during opening hours. Edge disk holds 48 h main + 7 days sub.
Backfill: outage < 2 min → nothing; 2 min–2 h → fetch recorded substream from edge, replay via file mode, mark
results *retrospective* (report only, no push); > 2 h → coverage gap in the report. Every alert → HD main-stream clip to S3.

## 5. Fleet ops
Enrol with a one-time code → site bundle over HTTPS. ONVIF WS-Discovery + `GetStreamUri` (no URL guessing),
ffprobe validation, set substream via ONVIF (fallback Hikvision ISAPI / Dahua CGI). Read-only per-site camera
user; back off on 401 (Hikvision locks after ~5). Signed release manifest, canary → 10% → all, after 20:00 IST,
auto-rollback on failed health gate. Alerts: SRT down > 2 min, loss > 5%, disk > 85%, clock drift > 1 s.

## 6. Latency budget (camera → phone)
Encode 0.15–0.3 s + SRT window 1.0 s + decode/sample 0.05–0.35 s + inference 0.08–0.2 s + push 0.5–3 s
≈ **1.8–5 s**, before the rule timers (zone 3 s, phone 10 s, no-adult 20 s), which dominate.
