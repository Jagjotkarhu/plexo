# Plexo CLI (`plexo-cli`)

> **Multi-network bandwidth bonding download accelerator in your terminal.**

Combines multiple active internet connections (e.g. **Hostel Wi-Fi + Mobile 5G via USB Tethering + Ethernet**) into a single parallel stream to boost download speeds for direct files and developer tools.

---

## Features
- **Parallel HTTP Range Slicing**: Binds concurrent download workers to distinct physical network interfaces (source IP socket binding).
- **Zero-Touch Local Proxy (`plexo proxy`)**: Run a local forward proxy (`127.0.0.1:8899`) to automatically accelerate `pip install`, `git clone`, `curl`, and browser downloads.
- **Rich Terminal UI**: Live dashboard showing real-time throughput per individual network adapter.
- **Dynamic Failover**: Auto-detects lagging or disconnected hotspots and re-routes pending chunks to healthy connections without corrupting files.
- **Sparse File Allocation**: Instant zero-overhead pre-allocation on disk with non-blocking multi-threaded byte writes.

---

## Installation

```bash
pip install plexo-cli
```

Or install in editable mode from source:
```bash
git clone https://github.com/Jagjotkarhu/plexo.git
cd plexo/packages/plexo-cli
pip install -e .
```

---

## Quickstart

### 1. Scan Network Adapters
```bash
plexo adapters
```
Lists all discovered network interfaces (Wi-Fi, USB cellular tether, Ethernet), their local IPs, and connectivity status.

### 2. Download a File with Aggregated Bandwidth
```bash
plexo download https://example.com/largefile.zip -o largefile.zip
```

### 3. Accelerate `pip` or Terminal Tools
Start the local proxy:
```bash
plexo proxy --port 8899
```
Then run any tool using the proxy:
```bash
pip install torch --proxy http://127.0.0.1:8899
```

---

## License
MIT © Anmol Kapil & Jagjot Singh
