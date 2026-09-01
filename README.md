# Market Watchlist TUI

A live terminal market watchlist with no third-party dependencies.

## Installation

```bash
pip install -e .
```

## Usage

```bash
watchlist          # Run the TUI
watchlist --check  # Test data fetching without TUI
```

## Controls

- `↑/↓` or `j/k` - Select asset
- `←/→` or `h/l` - Change time range
- `r` - Refresh data
- `q` or `Esc` - Quit
- `Enter` - Toggle detail view (narrow terminals)