package za.helpdesk.app;

import android.annotation.SuppressLint;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.text.InputType;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.ProgressBar;
import android.widget.Toast;

import androidx.activity.OnBackPressedCallback;
import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.appcompat.app.AlertDialog;
import androidx.appcompat.app.AppCompatActivity;

/**
 * Thin native shell around the helpdesk web app: sign-in cookie is kept, files can be attached,
 * attachments download to the phone, and WhatsApp / mail / phone links open the right app.
 */
public class MainActivity extends AppCompatActivity {

    private static final String DEFAULT_URL = "https://desktop-obtdcvt.tail973ab1.ts.net/Helpdesk/";
    private static final String ACTION_CHANGE_ADDRESS = "za.helpdesk.app.CHANGE_ADDRESS";

    private SharedPreferences prefs;
    private WebView web;
    private ProgressBar progress;
    private View errorView;
    private boolean failed;
    private ValueCallback<Uri[]> filePathCallback;
    private ActivityResultLauncher<Intent> chooserLauncher;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        prefs = getSharedPreferences("helpdesk", MODE_PRIVATE);
        web = findViewById(R.id.web);
        progress = findViewById(R.id.progress);
        errorView = findViewById(R.id.error);

        chooserLauncher = registerForActivityResult(new ActivityResultContracts.StartActivityForResult(), result -> {
            if (filePathCallback == null) return;
            Uri[] uris = WebChromeClient.FileChooserParams.parseResult(result.getResultCode(), result.getData());
            filePathCallback.onReceiveValue(uris);
            filePathCallback = null;
        });

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setUserAgentString(s.getUserAgentString() + " HelpdeskApp/1.0");
        CookieManager.getInstance().setAcceptCookie(true);

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
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                CookieManager.getInstance().flush();
                if (!failed) errorView.setVisibility(View.GONE);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    failed = true;
                    errorView.setVisibility(View.VISIBLE);
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
                try {
                    chooserLauncher.launch(params.createIntent());
                } catch (ActivityNotFoundException e) {
                    filePathCallback = null;
                    return false;
                }
                return true;
            }
        });

        findViewById(R.id.retry).setOnClickListener(v -> {
            errorView.setVisibility(View.GONE);
            web.loadUrl(serverUrl());
        });
        findViewById(R.id.change).setOnClickListener(v -> askAddress());

        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                goBackInApp();
            }
        });

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
        } else {
            web.loadUrl(serverUrl());
        }
        handleIntent(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleIntent(intent);
    }

    private void handleIntent(Intent intent) {
        if (intent != null && ACTION_CHANGE_ADDRESS.equals(intent.getAction())) {
            askAddress();
        }
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

    private boolean sameHost(Uri u) {
        String host = Uri.parse(serverUrl()).getHost();
        return host != null && host.equalsIgnoreCase(u.getHost());
    }

    private void openExternal(Uri u) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, u));
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, R.string.no_app, Toast.LENGTH_SHORT).show();
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
            req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETION);
            req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
            DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
            dm.enqueue(req);
            Toast.makeText(this, R.string.downloading, Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            openExternal(Uri.parse(url));
        }
    }

    private void askAddress() {
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
                    if (!s.startsWith("http")) s = "https://" + s;
                    if (!s.endsWith("/")) s = s + "/";
                    prefs.edit().putString("url", s).apply();
                    errorView.setVisibility(View.GONE);
                    web.loadUrl(s);
                })
                .setNegativeButton(android.R.string.cancel, null)
                .show();
    }

    /** Back closes an open pop-up or ticket first, then goes back in history, then leaves the app. */
    private void goBackInApp() {
        String js = "(function(){var m=document.getElementById('modal');"
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
