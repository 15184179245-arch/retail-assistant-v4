package com.retail.smartops.core.network

/** Strict JSON subset for the two login success objects. No success field permits a number or array. */
internal fun readLoginSuccessJson(raw: String): Map<String, Any?>? = try {
    LoginSuccessJson(raw).read()
} catch (_: IllegalArgumentException) {
    null
}

private class LoginSuccessJson(private val text: String) {
    private var at = 0

    fun read(): Map<String, Any?> {
        val value = readObject(depth = 0)
        skipSpace()
        require(at == text.length)
        return value
    }

    private fun readObject(depth: Int): Map<String, Any?> {
        require(depth <= 1)
        expect('{')
        val entries = linkedMapOf<String, Any?>()
        if (take('}')) return entries
        while (true) {
            val key = readString()
            require(!entries.containsKey(key))
            expect(':')
            skipSpace()
            entries[key] = when (peek()) {
                '"' -> readString()
                '{' -> readObject(depth + 1)
                't' -> readLiteral("true", true)
                'f' -> readLiteral("false", false)
                'n' -> readLiteral("null", null)
                else -> throw IllegalArgumentException()
            }
            if (take('}')) return entries
            expect(',')
        }
    }

    private fun readString(): String {
        expect('"')
        return buildString {
            while (at < text.length) {
                val ch = text[at++]
                when {
                    ch == '"' -> return@buildString
                    ch == '\\' -> append(readEscape())
                    ch < ' ' -> throw IllegalArgumentException()
                    else -> append(ch)
                }
            }
            throw IllegalArgumentException()
        }
    }

    private fun readEscape(): Char {
        require(at < text.length)
        return when (val escape = text[at++]) {
            '"', '/', '\\' -> escape
            'b' -> '\b'
            'f' -> '\u000c'
            'n' -> '\n'
            'r' -> '\r'
            't' -> '\t'
            'u' -> readHexEscape()
            else -> throw IllegalArgumentException()
        }
    }

    private fun readHexEscape(): Char {
        var code = 0
        repeat(4) {
            val hex = when (val ch = peek()) {
                in '0'..'9' -> ch - '0'
                in 'a'..'f' -> ch - 'a' + 10
                in 'A'..'F' -> ch - 'A' + 10
                else -> throw IllegalArgumentException()
            }
            at++
            code = code * 16 + hex
        }
        return code.toChar()
    }

    private fun <T> readLiteral(word: String, value: T): T {
        require(text.startsWith(word, at))
        at += word.length
        return value
    }

    private fun peek(): Char = text.getOrElse(at) { '\u0000' }
    private fun skipSpace() { while (peek() in " \t\n\r") at++ }
    private fun take(ch: Char): Boolean {
        skipSpace()
        if (peek() != ch) return false
        at++
        return true
    }
    private fun expect(ch: Char) { require(take(ch)) }
}
