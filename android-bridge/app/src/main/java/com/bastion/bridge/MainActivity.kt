package com.bastion.bridge

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.bastion.bridge.databinding.ActivityMainBinding

private const val TERMUX_PACKAGE = "com.termux"
private const val TERMUX_SERVICE = "com.termux.app.RunCommandService"
private const val TERMUX_RUN_COMMAND_PERMISSION = "com.termux.permission.RUN_COMMAND"
private const val ACTION_RUN_COMMAND = "com.termux.RUN_COMMAND"
private const val EXTRA_COMMAND_PATH = "com.termux.RUN_COMMAND_PATH"
private const val EXTRA_ARGUMENTS = "com.termux.RUN_COMMAND_ARGUMENTS"
private const val EXTRA_WORKDIR = "com.termux.RUN_COMMAND_WORKDIR"
private const val EXTRA_BACKGROUND = "com.termux.RUN_COMMAND_BACKGROUND"
private const val EXTRA_SESSION_ACTION = "com.termux.RUN_COMMAND_SESSION_ACTION"
private const val EXTRA_COMMAND_LABEL = "com.termux.RUN_COMMAND_LABEL"
private const val SESSION_OPEN_NEW_AND_SWITCH = "0"

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private var launchRequest: LaunchRequest? = null

    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) {
                runInTermux()
            } else {
                binding.statusText.text = getString(R.string.status_need_permission)
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        launchRequest = LaunchRequest.fromIntent(intent)
        bindUi()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        launchRequest = LaunchRequest.fromIntent(intent)
        bindUi()
    }

    private fun bindUi() {
        val request = launchRequest
        if (request == null) {
            binding.summaryText.text = ""
            binding.commandText.text = ""
            binding.statusText.text = getString(R.string.status_missing_link)
            binding.confirmButton.isEnabled = false
        } else {
            binding.summaryText.text = request.summary()
            binding.commandText.text = request.command
            binding.statusText.text = when {
                !isTermuxInstalled() -> getString(R.string.status_need_termux)
                !hasRunCommandPermission() -> getString(R.string.status_need_permission)
                else -> getString(R.string.status_ready)
            }
            binding.confirmButton.isEnabled = true
        }

        binding.cancelButton.setOnClickListener { finish() }
        binding.confirmButton.setOnClickListener { handleConfirm() }
    }

    private fun handleConfirm() {
        if (launchRequest == null) {
            finish()
            return
        }

        if (!isTermuxInstalled()) {
            binding.statusText.text = getString(R.string.status_need_termux)
            startActivity(
                Intent(
                    Intent.ACTION_VIEW,
                    Uri.parse("https://github.com/termux/termux-app/releases")
                )
            )
            return
        }

        if (!hasRunCommandPermission()) {
            permissionLauncher.launch(TERMUX_RUN_COMMAND_PERMISSION)
            return
        }

        runInTermux()
    }

    private fun runInTermux() {
        val request = launchRequest ?: return
        copyCommand(request.command)

            val intent = Intent().apply {
            setClassName(TERMUX_PACKAGE, TERMUX_SERVICE)
            action = ACTION_RUN_COMMAND
            putExtra(EXTRA_COMMAND_PATH, "\$PREFIX/bin/bash")
            putExtra(EXTRA_ARGUMENTS, arrayOf("-lc", request.command))
            putExtra(EXTRA_WORKDIR, "~/")
            putExtra(EXTRA_BACKGROUND, false)
            putExtra(EXTRA_SESSION_ACTION, SESSION_OPEN_NEW_AND_SWITCH)
            putExtra(EXTRA_COMMAND_LABEL, "Bastion ${request.host}")
        }

        try {
            startService(intent)
            launchTermuxUi()
            binding.statusText.text = getString(R.string.status_sent)
            Toast.makeText(this, getString(R.string.status_sent), Toast.LENGTH_SHORT).show()
            finish()
        } catch (e: Exception) {
            binding.statusText.text = e.message ?: getString(R.string.status_need_permission)
        }
    }

    private fun launchTermuxUi() {
        val launchIntent = packageManager.getLaunchIntentForPackage(TERMUX_PACKAGE)
        if (launchIntent != null) {
            launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            startActivity(launchIntent)
        }
    }

    private fun copyCommand(command: String) {
        val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText("Bastion command", command))
    }

    private fun hasRunCommandPermission(): Boolean {
        return ContextCompat.checkSelfPermission(
            this,
            TERMUX_RUN_COMMAND_PERMISSION
        ) == PackageManager.PERMISSION_GRANTED
    }

    private fun isTermuxInstalled(): Boolean {
        return try {
            packageManager.getPackageInfo(TERMUX_PACKAGE, 0)
            true
        } catch (_: Exception) {
            false
        }
    }
}

private data class LaunchRequest(
    val host: String,
    val user: String,
    val session: String,
    val mode: String,
    val jumpbox: String,
    val via: String,
    val port: String,
    val command: String,
) {
    fun summary(): String {
        return if (via == "tailscale") {
            "Target: $host\nVia: $jumpbox\nMode: $mode\nSession: $session"
        } else {
            "Target: $user@$host:$port\nMode: $mode\nSession: $session"
        }
    }

    companion object {
        fun fromIntent(intent: Intent?): LaunchRequest? {
            val data = intent?.data ?: return null
            if (data.scheme != "bastion" || data.host != "connect") return null

            val host = data.getQueryParameter("host").orEmpty()
            val user = data.getQueryParameter("user").orEmpty().ifBlank { "root" }
            val session = data.getQueryParameter("session").orEmpty().ifBlank { host }
            val mode = data.getQueryParameter("mode").orEmpty().ifBlank { "resume" }
            val jumpbox = data.getQueryParameter("jumpbox").orEmpty()
            val via = data.getQueryParameter("via").orEmpty().ifBlank { "tailscale" }
            val port = data.getQueryParameter("port").orEmpty().ifBlank { "22" }
            if (host.isBlank()) return null

            val command = buildCommand(host, user, session, mode, jumpbox, via, port)
            return LaunchRequest(host, user, session, mode, jumpbox, via, port, command)
        }

        private fun buildCommand(
            host: String,
            user: String,
            session: String,
            mode: String,
            jumpbox: String,
            via: String,
            port: String,
        ): String {
            return if (via == "tailscale") {
                if (mode == "direct") {
                    "ssh -t ${shellQuote(jumpbox)} \"bastion-ssh ${shellQuote(host)} ${shellQuote(user)}\""
                } else {
                    val tmux = if (mode == "new") "tmux new-session -s" else "tmux new-session -A -s"
                    "ssh -t ${shellQuote(jumpbox)} \"$tmux ${shellQuote(session)} bastion-ssh ${shellQuote(host)} ${shellQuote(user)}\""
                }
            } else {
                if (mode == "direct") {
                    "ssh -t -p $port ${shellQuote("$user@$host")}"
                } else {
                    val tmux = if (mode == "new") "tmux new-session -s" else "tmux new-session -A -s"
                    "ssh -t -p $port ${shellQuote("$user@$host")} \"$tmux ${shellQuote(session)}\""
                }
            }
        }

        private fun shellQuote(value: String): String {
            return "'" + value.replace("'", "'\\''") + "'"
        }
    }
}
