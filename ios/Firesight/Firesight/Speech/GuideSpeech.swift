import AVFoundation

final class GuideSpeech {
    private let synth = AVSpeechSynthesizer()
    private var last = ""

    func stop() {
        last = ""
        synth.stopSpeaking(at: .immediate)
    }

    func say(_ text: String) {
        let line = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if line.isEmpty || line == last { return }
        last = line
        synth.stopSpeaking(at: .immediate)
        let utterance = AVSpeechUtterance(string: line)
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate * 0.92
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        synth.speak(utterance)
    }
}
