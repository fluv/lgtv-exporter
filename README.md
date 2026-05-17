# lgtv-exporter

Prometheus exporter for LG WebOS TVs. Connects via the local SSAP WebSocket
protocol using [bscpylgtv](https://github.com/chros73/bscpylgtv) and exposes
TV state as metrics.

## Metrics

| Metric | Type | Description |
|---|---|---|
| `lgtv_connected` | gauge | 1 if the exporter has an active connection |
| `lgtv_on` | gauge | 1 when TV is in Active power state |
| `lgtv_volume` | gauge | Current volume (0–100) |
| `lgtv_muted` | gauge | 1 if muted |
| `lgtv_app_info` | gauge/label | Current foreground app ID |
| `lgtv_input_info` | gauge/label | Current input source |
| `lgtv_sound_output_info` | gauge/label | Audio output device |
| `lgtv_picture_info` | gauge/label | Picture mode and HDR type |
| `lgtv_picture_*` | gauge | Individual numeric picture settings |
| `lgtv_channel_info` | gauge/label | Channel and programme (live TV only) |
| `lgtv_power_info` | gauge/label | Raw power state string |
| `lgtv_build_info` | gauge/label | Model and firmware version |

`lgtv_connected 0` means the TV is off or unreachable — other metrics are not
updated when disconnected but retain their last known values.

## Setup

### 1. Pair with the TV (one-time)

Run the pairing command while the TV is on. A prompt will appear on screen — accept it.

```
kubectl run lgtv-pair --rm -it \
  --image=ghcr.io/fluv/lgtv-exporter:main \
  --restart=Never \
  --env="LGTV_IP=192.168.1.185" \
  -- python3 lgtv_exporter.py --pair
```

Copy the printed key and create the Kubernetes secret:

```
kubectl create secret generic lgtv-client-key \
  --from-literal=client-key=<key> \
  -n lifestyle
```

### 2. Deploy

The deployment in `fluv/kube` picks up the secret automatically. Ensure the
`lifestyle` namespace exists and the secret is present before syncing.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `LGTV_IP` | — | TV IP address or hostname (required) |
| `LGTV_CLIENT_KEY` | — | SSAP client key from pairing (required) |
| `PORT` | `9095` | HTTP port for `/metrics` |
| `RECONNECT_DELAY` | `30` | Seconds between reconnect attempts |
