package za.helpdesk.app;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.DownloadManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.text.InputType;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import androidx.activity.OnBackPressedCallback;
import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.appcompat.app.AlertDialog;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.content.FileProvider;
import androidx.core.content.pm.PackageInfoCompat;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.IOException;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.text.DateFormat;
import java.util.Date;

/**
 * Native shell around the helpdesk web app: the sign-in cookie is kept, photos and files can be attached,
 * attachments download to the phone, WhatsApp / mail / phone links open the right app, a new-ticket
 * notification is shown when the app opens, the last known tickets are shown when the server can't be
 * reached, and a newer APK from the GitHub releases is offered as an update.
 */
public class MainActivity extends AppCompatActivity {

    private static final String DEFAULT_URL = "https://desktop-obtdcvt.tail973ab1.ts.net/Helpdesk/";
    private static final String ACTION_NEW_TICKET = "za.helpdesk.app.NEW_TICKET";
    private static final String ACTION_MY_TICKETS = "za.helpdesk.app.MY_TICKETS";
    private static final String RELEASES_URL = "https://api.github.com/repos/MariusCraill/Helpdesk/releases/latest";
    private static final String CHANNEL_ID = "tickets";
    private static final long LOAD_TIMEOUT_MS = 20000;
    private static final long UPDATE_CHECK_EVERY_MS = 6L * 60 * 60 * 1000;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private SharedPreferences prefs;
    private WebView web;
    private ProgressBar progress;
    private View errorView;
    private TextView errorTitle, errorText, errorServer, snapHeader, snapBody;
    private boolean failed;
    private ValueCallback<Uri[]> filePathCallback;
    private ActivityResultLauncher<Intent> chooserLauncher;
    private ActivityResultLauncher<String> notifPermission;
    private Uri cameraUri;
    private File cameraFile;
    private String[] pendingNotification;
    private ConnectivityManager connectivity;
    private ConnectivityManager.NetworkCallback networkCallback;

    private final Runnable loadTimeout = () -> {
        if (web != null && web.getProgress() < 100 && !failed) {
            web.stopLoading();
            showError("Taking too long", "The helpdesk is not answering. The PC may be off, or your connection is very slow.");
        }
    };

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        prefs = getSharedPreferences("helpdesk", MODE_PRIVATE);
        connectivity = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        web = findViewById(R.id.web);
        progress = findViewById(R.id.progress);
        errorView = findViewById(R.id.error);
        errorTitle = findViewById(R.id.errorTitle);
        errorText = findViewById(R.id.errorText);
        errorServer = findViewById(R.id.errorServer);
        snapHeader = findViewById(R.id.snapHeader);
        snapBody = findViewById(R.id.snapBody);

        chooserLauncher = registerForActivityResult(new ActivityResultContracts.StartActivityForResult(), result -> {
            if (filePathCallback == null) return;
            Uri[] uris = null;
            if (result.getResultCode() == RESULT_OK) {
                Intent data = result.getData();
                boolean picked = data != null && (data.getData() != null || data.getClipData() != null);
                if (picked) {
                    uris = WebChromeClient.FileChooserParams.parseResult(result.getResultCode(), data);
                } else if (cameraUri != null && cameraFile != null && cameraFile.length() > 0) {
                    uris = new Uri[]{cameraUri};   // the photo the camera just took
                }
            }
            if (uris == null && cameraFile != null) cameraFile.delete();
            filePathCallback.onReceiveValue(uris);
            filePathCallback = null;
            cameraUri = null;
            cameraFile = null;
        });

        notifPermission = registerForActivityResult(new ActivityResultContracts.RequestPermission(), granted -> {
            if (granted && pendingNotification != null) postNotification(pendingNotification[0], pendingNotification[1]);
            pendingNotification = null;
        });

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setUserAgentString(s.getUserAgentString() + " HelpdeskApp/1.1");
        CookieManager.getInstance().setAcceptCookie(true);

        // Lets the page ask the app for things only the app can do (it does not exist in a normal browser).
        web.addJavascriptInterface(new Object() {
            @android.webkit.JavascriptInterface
            public void changeServer() {
                runOnUiThread(MainActivity.this::askAddress);
            }

            @android.webkit.JavascriptInterface
            public void setRole(String role) {
                prefs.edit().putBoolean("admin", "admin".equals(role)).apply();
                runOnUiThread(() -> findViewById(R.id.change).setVisibility(isAdmin() ? View.VISIBLE : View.GONE));
            }

            /** Compact list of the open tickets, shown on the error screen when the server can't be reached. */
            @android.webkit.JavascriptInterface
            public void saveSnapshot(String json) {
                if (json == null || json.length() > 60000) return;
                prefs.edit().putString("snap", json).putLong("snapAt", System.currentTimeMillis()).apply();
            }

            @android.webkit.JavascriptInterface
            public void notify(String title, String text) {
                runOnUiThread(() -> postNotification(title, text));
            }
        }, "HelpdeskNative");

        web.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) ->
                download(url, contentDisposition, mimeType));

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri u = request.getUrl();
                String scheme = u.getScheme();
                if (("http".equals(scheme) || "https".equals(scheme)) && sameHost(u)) {
                    return false; // stay inside the app
                }
                openExternal(u);   // wa.me, mailto:, tel:, other sites ...
                return true;
            }

            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                failed = false;
                ui.removeCallbacks(loadTimeout);
                ui.postDelayed(loadTimeout, LOAD_TIMEOUT_MS);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                ui.removeCallbacks(loadTimeout);
                CookieManager.getInstance().flush();
                if (!failed) errorView.setVisibility(View.GONE);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (!request.isForMainFrame()) return;
                ui.removeCallbacks(loadTimeout);
                switch (error.getErrorCode()) {
                    case ERROR_HOST_LOOKUP:
                    case ERROR_CONNECT:
                    case ERROR_TIMEOUT:
                        if (!online()) showOffline();
                        else showError("Can't reach the helpdesk",
                                "The helpdesk PC may be switched off, or its connection is down. Try again in a moment.");
                        break;
                    case ERROR_FAILED_SSL_HANDSHAKE:
                        showError("Secure connection failed", "The secure link to the helpdesk could not be set up. Check the server address.");
                        break;
                    default:
                        if (!online()) showOffline();
                        else showError("Something went wrong", "The page could not be loaded (" + error.getDescription() + ").");
                }
            }

            @Override
            public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
                if (request.isForMainFrame() && response.getStatusCode() >= 500) {
                    ui.removeCallbacks(loadTimeout);
                    showError("The helpdesk is not responding",
                            "The server answered with an error (" + response.getStatusCode() + "). The helpdesk program on the PC may be stopped or restarting.");
                }
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                progress.setProgress(newProgress);
                progress.setVisibility(newProgress < 100 ? View.VISIBLE : View.GONE);
            }

            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (filePathCallback != null) filePathCallback.onReceiveValue(null);
                filePathCallback = callback;
                Intent camera = acceptsImages(params.getAcceptTypes()) ? cameraIntent() : null;
                Intent launch;
                if (params.isCaptureEnabled() && camera != null) {
                    launch = camera;                       // "Photo" button: straight to the camera
                } else {
                    launch = Intent.createChooser(params.createIntent(), "Choose a file");
                    if (camera != null) launch.putExtra(Intent.EXTRA_INITIAL_INTENTS, new Intent[]{camera});
                }
                try {
                    chooserLauncher.launch(launch);
                } catch (ActivityNotFoundException e) {
                    filePathCallback = null;
                    return false;
                }
                return true;
            }
        });

        findViewById(R.id.retry).setOnClickListener(v -> reload());
        findViewById(R.id.change).setOnClickListener(v -> askAddress());
        findViewById(R.id.change).setVisibility(isAdmin() ? View.VISIBLE : View.GONE);

        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                goBackInApp();
            }
        });

        // When the connection comes back, try again by itself.
        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network network) {
                ui.post(() -> {
                    if (failed) reload();
                });
            }
        };
        try {
            connectivity.registerDefaultNetworkCallback(networkCallback);
        } catch (RuntimeException ignored) {
            networkCallback = null;
        }

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
        } else {
            web.loadUrl(serverUrl() + hashFor(getIntent()));
        }
        checkForUpdate();
    }

    @Override
    protected void onDestroy() {
        ui.removeCallbacks(loadTimeout);
        if (networkCallback != null) {
            try {
                connectivity.unregisterNetworkCallback(networkCallback);
            } catch (RuntimeException ignored) {
            }
        }
        super.onDestroy();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        String hash = hashFor(intent);
        if (!hash.isEmpty() && !failed) {
            web.evaluateJavascript("location.hash='" + hash + "'", null);
        }
    }

    /** Home-screen shortcuts open the web app with #new or #mine, which the page understands. */
    private String hashFor(Intent intent) {
        String a = intent == null ? null : intent.getAction();
        if (ACTION_NEW_TICKET.equals(a)) return "#new";
        if (ACTION_MY_TICKETS.equals(a)) return "#mine";
        return "";
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush();
    }

    private String serverUrl() {
        return prefs.getString("url", DEFAULT_URL);
    }

    private boolean isAdmin() {
        return prefs.getBoolean("admin", false);
    }

    private boolean sameHost(Uri u) {
        String host = Uri.parse(serverUrl()).getHost();
        return host != null && host.equalsIgnoreCase(u.getHost());
    }

    private void reload() {
        errorView.setVisibility(View.GONE);
        failed = false;
        web.loadUrl(serverUrl());
    }

    private void openExternal(Uri u) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, u));
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, R.string.no_app, Toast.LENGTH_SHORT).show();
        }
    }

    // ---------------------------------------------------------------- errors / offline

    private boolean online() {
        try {
            Network n = connectivity.getActiveNetwork();
            if (n == null) return false;
            NetworkCapabilities c = connectivity.getNetworkCapabilities(n);
            return c != null && c.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
        } catch (RuntimeException e) {
            return true;
        }
    }

    private void showOffline() {
        showError("You're offline", "Turn on Wi-Fi or mobile data. The app will reconnect by itself.");
    }

    private void showError(String title, String detail) {
        failed = true;
        errorTitle.setText(title);
        errorText.setText(detail);
        errorServer.setText("Server: " + serverUrl());
        fillSnapshot();
        errorView.setVisibility(View.VISIBLE);
    }

    /** Shows the tickets the app saw last time, so agents can still see who is waiting while offline. */
    private void fillSnapshot() {
        String json = prefs.getString("snap", null);
        if (json == null) {
            snapHeader.setVisibility(View.GONE);
            snapBody.setVisibility(View.GONE);
            return;
        }
        try {
            JSONArray arr = new JSONArray(json);
            StringBuilder sb = new StringBuilder();
            int n = Math.min(arr.length(), 15);
            for (int i = 0; i < n; i++) {
                JSONArray t = arr.getJSONArray(i);
                sb.append("#").append(t.getInt(0)).append("  ").append(t.getString(1))
                        .append("\n      ").append(t.getString(2)).append('\n');
            }
            if (arr.length() > n) sb.append("… and ").append(arr.length() - n).append(" more");
            String at = DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.SHORT)
                    .format(new Date(prefs.getLong("snapAt", 0)));
            snapHeader.setText("Last known open tickets (saved " + at + ")");
            snapBody.setText(sb.toString().trim());
            snapHeader.setVisibility(View.VISIBLE);
            snapBody.setVisibility(View.VISIBLE);
        } catch (Exception e) {
            snapHeader.setVisibility(View.GONE);
            snapBody.setVisibility(View.GONE);
        }
    }

    // ---------------------------------------------------------------- camera / files

    private boolean acceptsImages(String[] types) {
        if (types == null || types.length == 0) return true;
        for (String t : types) {
            if (t == null || t.trim().isEmpty() || t.startsWith("image") || t.contains("*/*")) return true;
        }
        return false;
    }

    private Intent cameraIntent() {
        try {
            File dir = new File(getCacheDir(), "uploads");
            //noinspection ResultOfMethodCallIgnored
            dir.mkdirs();
            File[] old = dir.listFiles();
            if (old != null) {
                for (File f : old) {
                    if (System.currentTimeMillis() - f.lastModified() > 24L * 60 * 60 * 1000) f.delete();
                }
            }
            cameraFile = File.createTempFile("photo_", ".jpg", dir);
            cameraUri = FileProvider.getUriForFile(this, getPackageName() + ".files", cameraFile);
            Intent i = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
            i.putExtra(MediaStore.EXTRA_OUTPUT, cameraUri);
            i.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_READ_URI_PERMISSION);
            if (i.resolveActivity(getPackageManager()) == null) {
                cameraUri = null;
                cameraFile = null;
                return null;
            }
            return i;
        } catch (IOException | RuntimeException e) {
            cameraUri = null;
            cameraFile = null;
            return null;
        }
    }

    private void download(String url, String contentDisposition, String mimeType) {
        try {
            DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
            String cookie = CookieManager.getInstance().getCookie(url);
            if (cookie != null) req.addRequestHeader("Cookie", cookie);
            req.addRequestHeader("User-Agent", web.getSettings().getUserAgentString());
            String name = URLUtil.guessFileName(url, contentDisposition, mimeType);
            req.setTitle(name);
            req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
            DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
            dm.enqueue(req);
            Toast.makeText(this, R.string.downloading, Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            openExternal(Uri.parse(url));
        }
    }

    // ---------------------------------------------------------------- notifications

    private void postNotification(String title, String text) {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            pendingNotification = new String[]{title, text};
            notifPermission.launch(Manifest.permission.POST_NOTIFICATIONS);
            return;
        }
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;
        nm.createNotificationChannel(new NotificationChannel(CHANNEL_ID, "New tickets", NotificationManager.IMPORTANCE_HIGH));
        Intent open = new Intent(this, MainActivity.class).setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification n = new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_stat_ticket)
                .setContentTitle(title)
                .setContentText(text)
                .setAutoCancel(true)
                .setContentIntent(pi)
                .build();
        nm.notify(1, n);
    }

    // ---------------------------------------------------------------- self-update

    /** Looks at the newest release on GitHub (at most every 6 hours) and offers to download it. */
    private void checkForUpdate() {
        long last = prefs.getLong("updChecked", 0);
        if (System.currentTimeMillis() - last < UPDATE_CHECK_EVERY_MS) return;
        new Thread(() -> {
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(RELEASES_URL).openConnection();
                c.setConnectTimeout(8000);
                c.setReadTimeout(8000);
                c.setRequestProperty("Accept", "application/vnd.github+json");
                if (c.getResponseCode() != 200) return;
                StringBuilder sb = new StringBuilder();
                try (BufferedReader r = new BufferedReader(new InputStreamReader(c.getInputStream()))) {
                    String line;
                    while ((line = r.readLine()) != null) sb.append(line);
                }
                JSONObject rel = new JSONObject(sb.toString());
                int remote = Integer.parseInt(rel.getString("tag_name").replaceAll("\\D", ""));
                String apk = null;
                JSONArray assets = rel.getJSONArray("assets");
                for (int i = 0; i < assets.length(); i++) {
                    JSONObject a = assets.getJSONObject(i);
                    if (a.getString("name").endsWith(".apk")) apk = a.getString("browser_download_url");
                }
                prefs.edit().putLong("updChecked", System.currentTimeMillis()).apply();
                long local = PackageInfoCompat.getLongVersionCode(getPackageManager().getPackageInfo(getPackageName(), 0));
                if (apk == null || remote <= local || prefs.getInt("updSkip", 0) >= remote) return;
                final String url = apk;
                final int ver = remote;
                runOnUiThread(() -> offerUpdate(url, ver));
            } catch (Exception ignored) {
                // offline or GitHub unreachable: try again next time
            }
        }).start();
    }

    private void offerUpdate(String url, int version) {
        if (isFinishing() || isDestroyed()) return;
        new AlertDialog.Builder(this)
                .setTitle("Update available")
                .setMessage("A newer version of the Helpdesk app (build " + version + ") is ready. Download it now? "
                        + "When it finishes, tap the download notification to install.")
                .setPositiveButton("Download", (d, w) -> {
                    try {
                        DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
                        req.setTitle("Helpdesk app update");
                        req.setMimeType("application/vnd.android.package-archive");
                        req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                        req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, "Helpdesk-" + version + ".apk");
                        ((DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE)).enqueue(req);
                        Toast.makeText(this, R.string.downloading, Toast.LENGTH_SHORT).show();
                    } catch (Exception e) {
                        openExternal(Uri.parse(url));
                    }
                })
                .setNegativeButton("Later", (d, w) -> prefs.edit().putInt("updSkip", version).apply())
                .show();
    }

    // ---------------------------------------------------------------- server address (admins only)

    private void askAddress() {
        if (!isAdmin()) {
            Toast.makeText(this, "Only an administrator can change the server address", Toast.LENGTH_LONG).show();
            return;
        }
        final EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(serverUrl());
        new AlertDialog.Builder(this)
                .setTitle(R.string.change_address)
                .setMessage(R.string.address_hint)
                .setView(input)
                .setPositiveButton(android.R.string.ok, (d, w) -> {
                    String s = input.getText().toString().trim();
                    if (s.isEmpty()) return;
                    if (!s.startsWith("http")) s = (s.matches("^[0-9.]+(:[0-9]+)?(/.*)?$") ? "http://" : "https://") + s;
                    if (!s.endsWith("/")) s = s + "/";
                    prefs.edit().putString("url", s).apply();
                    errorView.setVisibility(View.GONE);
                    web.loadUrl(s);
                })
                .setNegativeButton(android.R.string.cancel, null)
                .show();
    }

    /** Back closes an open menu, pop-up or ticket first, then goes back in history, then leaves the app. */
    private void goBackInApp() {
        if (failed) {
            finish();
            return;
        }
        String js = "(function(){var mw=document.getElementById('moreWrap');"
                + "if(mw&&mw.classList.contains('on')){mw.classList.remove('on');return 'x';}"
                + "var m=document.getElementById('modal');"
                + "if(m&&m.classList.contains('on')){closeModal();return 'x';}"
                + "var p=document.getElementById('v-tickets');var b=document.getElementById('backBtn');"
                + "if(p&&b&&p.dataset.pane==='detail'&&b.offsetParent!==null){b.click();return 'x';}"
                + "return '';})()";
        web.evaluateJavascript(js, value -> {
            if (value != null && value.contains("x")) return;
            if (web.canGoBack()) {
                web.goBack();
            } else {
                finish();
            }
        });
    }
}
