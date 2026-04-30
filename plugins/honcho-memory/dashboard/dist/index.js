/**
 * Honcho Memory Dashboard Plugin v0.3.0
 *
 * Dashboard visibility into Honcho AI-native memory using the Hermes Plugin SDK.
 * No build step needed — plain IIFE that uses SDK globals.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  var React = SDK.React;
  var Card = SDK.components.Card;
  var CardHeader = SDK.components.CardHeader;
  var CardTitle = SDK.components.CardTitle;
  var CardContent = SDK.components.CardContent;
  var Badge = SDK.components.Badge;
  var Button = SDK.components.Button;
  var Input = SDK.components.Input;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var cn = SDK.utils.cn;

  var API_BASE = "/api/plugins/honcho-memory";

  /* ───────────────────────────────────────────────────────────── */
  /* Detail Modal component                                        */
  /* ───────────────────────────────────────────────────────────── */

  function DetailModal(props) {
    var title = props.title || "Details";
    var onClose = props.onClose;
    var children = props.children;

    return React.createElement("div", {
      className: "fixed inset-0 bg-black/60",
      style: {
        zIndex: 2147483647,
        overflowY: "auto",
        padding: "8px",
      },
      onClick: onClose,
    },
      React.createElement("div", {
        className: "bg-background border border-border rounded-lg shadow-2xl flex flex-col",
        style: {
          position: "fixed",
          left: "50%",
          top: "calc(50% + 40px)",
          transform: "translate(-50%, -50%)",
          width: "min(960px, calc(100vw - 16px))",
          maxHeight: "calc(100dvh - 96px)",
          zIndex: 2147483647,
        },
        onClick: function (e) { e.stopPropagation(); },
      },
        // Header
        React.createElement("div", {
          className: "flex items-center justify-between px-4 py-3 border-b border-border flex-shrink-0",
        },
          React.createElement("h2", { className: "font-bold text-base" }, title),
          React.createElement(Button, {
            onClick: onClose,
            className: "px-2 py-1 text-xs",
          }, "✕")
        ),
        // Content
        React.createElement("div", {
          className: "flex-1 overflow-y-auto px-4 py-3",
        }, children)
      )
    );
  }

  /* ───────────────────────────────────────────────────────────── */
  /* Health status badge                                           */
  /* ───────────────────────────────────────────────────────────── */

  function HealthBadge(props) {
    var status = props.status;
    var message = props.message;
    var variant = status === "healthy" ? "default" : status === "not_configured" ? "secondary" : "destructive";
    var color = status === "healthy" ? "text-green-400" : status === "not_configured" ? "text-yellow-400" : "text-red-400";

    return React.createElement("div", { className: "flex flex-col gap-2" },
      React.createElement(Badge, { variant: variant }, status.toUpperCase()),
      message && React.createElement("span", { className: cn("text-xs", color) }, message)
    );
  }

  /* ───────────────────────────────────────────────────────────── */
  /* Stats overview card                                           */
  /* ───────────────────────────────────────────────────────────── */

  function StatsCard() {
    var _s = useState({ data: null, loading: true, error: null });
    var data = _s[0];
    var setData = _s[1];

    function load() {
      setData(function (p) { return Object.assign({}, p, { loading: true, error: null }); });
      SDK.fetchJSON(API_BASE + "/stats")
        .then(function (d) { setData(function (p) { return Object.assign({}, p, { data: d, loading: false }); }); })
        .catch(function (e) { setData(function (p) { return Object.assign({}, p, { loading: false, error: e.message || "Failed" }); }); });
    }

    useEffect(load, []);

    if (_s[0].loading) return React.createElement(CardContent, null, "Loading stats...");
    if (_s[0].error) return React.createElement(CardContent, null, React.createElement("span", { className: "text-red-400 text-sm" }, "Error: " + _s[0].error));
    if (!data) return React.createElement(CardContent, null, "No stats available");

    var queue = data.queue || {};
    return React.createElement(CardContent, { className: "grid grid-cols-2 gap-4" },
      React.createElement("div", { className: "flex flex-col" },
        React.createElement("span", { className: "text-2xl font-bold" }, data.peers_count || 0),
        React.createElement("span", { className: "text-xs text-muted-foreground" }, "Peers")
      ),
      React.createElement("div", { className: "flex flex-col" },
        React.createElement("span", { className: "text-2xl font-bold" }, data.total_messages || 0),
        React.createElement("span", { className: "text-xs text-muted-foreground" }, "Messages")
      ),
      React.createElement("div", { className: "flex flex-col col-span-2" },
        React.createElement("span", { className: "text-sm font-medium" }, "Queue"),
        React.createElement("div", { className: "flex gap-4 mt-1" },
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "Pending: " + (queue.pending_work_units || 0)),
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "Processing: " + (queue.in_progress_work_units || 0)),
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "Total: " + (queue.total_work_units || 0))
        )
      ),
      data.timestamp && React.createElement("span", { className: "text-xs text-muted-foreground col-span-2" }, "Updated: " + data.timestamp)
    );
  }

  /* ───────────────────────────────────────────────────────────── */
  /* Peers list (clickable)                                        */
  /* ───────────────────────────────────────────────────────────── */

  function PeersList(props) {
    var onOpen = props.onOpen;
    var _s = useState({ peers: [], loading: true, error: null });
    var state = _s[0];
    var setState = _s[1];

    function load() {
      setState(function (p) { return Object.assign({}, p, { loading: true, error: null }); });
      SDK.fetchJSON(API_BASE + "/peers")
        .then(function (d) { setState(function (p) { return Object.assign({}, p, { peers: d.peers || [], loading: false }); }); })
        .catch(function (e) { setState(function (p) { return Object.assign({}, p, { loading: false, error: e.message || "Failed" }); }); });
    }

    useEffect(load, []);

    if (state.loading) return React.createElement("div", null, "Loading peers...");
    if (state.error) return React.createElement("div", { className: "text-red-400 text-sm" }, "Error: " + state.error);
    if (!state.peers.length) return React.createElement("div", { className: "text-muted-foreground text-sm" }, "No peers found");

    return React.createElement("div", { className: "flex flex-col gap-3" },
      state.peers.map(function (peer) {
        var facts = peer.card || [];
        return React.createElement("div", {
          key: peer.id,
          className: "border border-border rounded-md p-3 flex flex-col gap-2 cursor-pointer hover:bg-accent/50 transition-colors",
          onClick: function () { onOpen(peer); },
        },
          React.createElement("div", { className: "flex items-center justify-between" },
            React.createElement("span", { className: "font-medium text-sm" }, peer.id),
            React.createElement(Badge, { variant: "outline" }, facts.length + " facts")
          ),
          facts.length > 0 && React.createElement("div", { className: "flex flex-col gap-1 mt-1" },
            facts.slice(0, 3).map(function (f, i) {
              return React.createElement("div", {
                key: i,
                className: "text-xs text-muted-foreground pl-2 border-l-2 border-border",
              }, (f.fact || "").substring(0, 100));
            })
          ),
          facts.length > 3 && React.createElement("div", { className: "text-xs text-muted-foreground italic pl-2" }, "+ " + (facts.length - 3) + " more… click to see all")
        );
      })
    );
  }

  /* ───────────────────────────────────────────────────────────── */
  /* Peer detail modal content                                     */
  /* ───────────────────────────────────────────────────────────── */

  function PeerDetailContent(peer) {
    var facts = peer.card || [];
    return React.createElement("div", { className: "flex flex-col gap-2" },
      React.createElement("div", { className: "flex items-center gap-2 mb-2" },
        React.createElement(Badge, { variant: "outline" }, facts.length + " facts")
      ),
      facts.map(function (f, i) {
        return React.createElement("div", {
          key: i,
          className: "border-l-2 border-border pl-3 py-1",
        },
          React.createElement("span", { className: "text-[10px] text-muted-foreground font-mono" }, "#" + (i + 1) + " "),
          React.createElement("span", { className: "text-sm" }, f.fact)
        );
      })
    );
  }

  /* ───────────────────────────────────────────────────────────── */
  /* Search result detail modal content                            */
  /* ───────────────────────────────────────────────────────────── */

  function SearchResultContent(item) {
    return React.createElement("div", { className: "flex flex-col gap-4" },
      React.createElement("div", { className: "grid grid-cols-2 gap-2 text-sm" },
        React.createElement("div", { className: "flex flex-col" },
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "Peer"),
          React.createElement("span", { className: "font-medium" }, item.peer_id || "(unknown)")
        ),
        React.createElement("div", { className: "flex flex-col" },
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "Session"),
          React.createElement("span", { className: "text-xs font-mono" }, item.session_id || "(unknown)")
        ),
        React.createElement("div", { className: "flex flex-col" },
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "Created"),
          React.createElement("span", { className: "text-xs" }, item.created_at || "(unknown)")
        ),
        React.createElement("div", { className: "flex flex-col" },
          React.createElement("span", { className: "text-xs text-muted-foreground" }, "Tokens"),
          React.createElement("span", { className: "text-xs" }, item.token_count || "(unknown)")
        )
      ),
      React.createElement("div", { className: "flex flex-col gap-1" },
        React.createElement("span", { className: "text-xs text-muted-foreground" }, "Content"),
        React.createElement("div", { className: "text-sm border border-border rounded-md p-3 bg-muted/20 whitespace-pre-wrap" }, item.content || "(empty)")
      )
    );
  }

  /* ───────────────────────────────────────────────────────────── */
  /* Search box (clickable results)                                */
  /* ───────────────────────────────────────────────────────────── */

  function SearchBox(props) {
    var onOpen = props.onOpen;
    var _q = useState("");
    var query = _q[0];
    var setQuery = _q[1];

    var _r = useState({ results: null, loading: false, error: null });
    var results = _r[0];
    var setResults = _r[1];

    function doSearch() {
      if (!query.trim()) return;
      setResults(function (p) { return Object.assign({}, p, { loading: true, error: null, results: null }); });

      fetch(API_BASE + "/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: query, limit: 10 })
      })
        .then(function (r) { return r.json(); })
        .then(function (d) { setResults(function (p) { return Object.assign({}, p, { loading: false, results: d.results || [] }); }); })
        .catch(function (e) { setResults(function (p) { return Object.assign({}, p, { loading: false, error: e.message || "Failed" }); }); });
    }

    function handleKey(e) {
      if (e.key === "Enter") doSearch();
    }

    return React.createElement("div", { className: "flex flex-col gap-3" },
      React.createElement("div", { className: "flex gap-2" },
        React.createElement(Input, {
          value: query,
          onChange: function (e) { setQuery(e.target.value); },
          onKeyPress: handleKey,
          placeholder: "Search memory semantically…",
          className: "flex-1",
        }),
        React.createElement(Button, {
          onClick: doSearch,
          disabled: _r[0].loading || !query.trim(),
          className: "px-4 py-2",
        }, _r[0].loading ? "Searching…" : "Search")
      ),

      _r[0].error && React.createElement("span", { className: "text-red-400 text-sm" }, "Error: " + _r[0].error),

      results.results && results.results.length > 0 && React.createElement("div", { className: "flex flex-col gap-2 mt-2" },
        results.results.slice(0, 10).map(function (r, i) {
          var preview = (r.content || "").substring(0, 120);
          var truncated = (r.content || "").length > 120;
          return React.createElement("div", {
            key: i,
            className: "border border-border rounded-md p-3 text-sm cursor-pointer hover:bg-accent/50 transition-colors",
            onClick: function () { onOpen(r); },
          },
            React.createElement("div", { className: "flex items-center justify-between mb-1" },
              React.createElement("span", { className: "text-muted-foreground text-xs" }, "Peer: " + (r.peer_id || "N/A")),
              truncated && React.createElement("span", { className: "text-muted-foreground text-xs italic" }, "…click to see full")
            ),
            React.createElement("div", { className: "text-xs" }, preview + (truncated ? "…" : ""))
          );
        })
      ),

      results.results && results.results.length === 0 && query && React.createElement("div", { className: "text-muted-foreground text-sm" }, "No results found.")
    );
  }

  /* ───────────────────────────────────────────────────────────── */
  /* Config card                                                   */
  /* ───────────────────────────────────────────────────────────── */

  function ConfigCard() {
    var _s = useState({ data: null, loading: true });
    var state = _s[0];
    var setState = _s[1];

    function load() {
      SDK.fetchJSON(API_BASE + "/config")
        .then(function (d) { setState({ data: d, loading: false }); })
        .catch(function () { setState({ data: null, loading: false }); });
    }

    useEffect(load, []);

    if (state.loading) return React.createElement(CardContent, null, "Loading config…");
    if (!state.data) return React.createElement(CardContent, null, React.createElement("span", { className: "text-muted-foreground text-sm" }, "Config unavailable"));

    var rows = Object.keys(state.data.config || {}).filter(function (k) { return !k.startsWith("has_"); });
    return React.createElement(CardContent, null,
      React.createElement("div", { className: "grid grid-cols-2 gap-2 text-sm" },
        rows.map(function (k) {
          return React.createElement("div", { key: k, className: "flex flex-col" },
            React.createElement("span", { className: "text-xs text-muted-foreground" }, k.replace(/_/g, " ")),
            React.createElement("span", { className: "font-mono text-xs" }, JSON.stringify(state.data.config[k]))
          );
        })
      )
    );
  }

  /* ───────────────────────────────────────────────────────────── */
  /* Main page                                                     */
  /* ───────────────────────────────────────────────────────────── */

  function HonchoMemoryPage() {
    var _health = useState({ status: "checking", message: null });
    var health = _health[0];
    var setHealth = _health[1];

    // Modal state
    var _modal = useState(null);
    var modal = _modal[0];
    var setModal = _modal[1];

    function loadHealth() {
      SDK.fetchJSON(API_BASE + "/health")
        .then(function (d) {
          setHealth({
            status: d.status,
            message: d.message || (d.status === "healthy" ? "Connected" : null),
          });
        })
        .catch(function () {
          setHealth({ status: "error", message: "Failed to reach Honcho" });
        });
    }

    useEffect(loadHealth, []);

    function openPeer(peer) {
      setModal({ type: "peer", data: peer });
    }

    function openSearchResult(result) {
      setModal({ type: "search", data: result });
    }

    function closeModal() {
      setModal(null);
    }

    return React.createElement("div", { className: "flex flex-col gap-6" },

      // Detail modals
      modal && modal.type === "peer" && React.createElement(DetailModal, {
        title: "Peer: " + modal.data.id,
        onClose: closeModal,
      }, PeerDetailContent(modal.data)),

      modal && modal.type === "search" && React.createElement(DetailModal, {
        title: "Search Result",
        onClose: closeModal,
      }, SearchResultContent(modal.data)),

      // Header
      React.createElement(Card, null,
        React.createElement(CardHeader, null,
          React.createElement("div", { className: "flex items-center justify-between" },
            React.createElement("div", { className: "flex items-center gap-3" },
              React.createElement(CardTitle, { className: "text-lg" }, "Honcho Memory"),
              React.createElement(Badge, { variant: "outline" }, "v0.3.0"),
            ),
            React.createElement(HealthBadge, { status: health.status, message: health.message }),
          )
        ),
        React.createElement(CardContent, null,
          React.createElement("p", { className: "text-sm text-muted-foreground" },
            "Dashboard for Honcho AI-native memory. ",
            React.createElement("span", { className: "text-blue-400" }, "Click any peer or search result to see full details.")
          )
        )
      ),

      // Stats row
      React.createElement(Card, null,
        React.createElement(CardHeader, null, React.createElement(CardTitle, { className: "text-base" }, "Overview")),
        React.createElement(StatsCard)
      ),

      // Peers
      React.createElement(Card, null,
        React.createElement(CardHeader, null,
          React.createElement("div", { className: "flex items-center gap-2" },
            React.createElement(CardTitle, { className: "text-base" }, "Peers"),
            React.createElement("span", { className: "text-xs text-muted-foreground" }, "(click to expand)")
          )
        ),
        React.createElement(CardContent, null, React.createElement(PeersList, { onOpen: openPeer }))
      ),

      // Search
      React.createElement(Card, null,
        React.createElement(CardHeader, null,
          React.createElement("div", { className: "flex items-center gap-2" },
            React.createElement(CardTitle, { className: "text-base" }, "Semantic Search"),
            React.createElement("span", { className: "text-xs text-muted-foreground" }, "(click to expand)")
          )
        ),
        React.createElement(CardContent, null, React.createElement(SearchBox, { onOpen: openSearchResult }))
      ),

      // Config
      React.createElement(Card, null,
        React.createElement(CardHeader, null, React.createElement(CardTitle, { className: "text-base" }, "Configuration")),
        React.createElement(ConfigCard)
      ),
    );
  }

  // Register
  window.__HERMES_PLUGINS__.register("honcho-memory", HonchoMemoryPage);
})();
