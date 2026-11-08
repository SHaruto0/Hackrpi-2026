import AVFoundation

final class GuideSpeech: NSObject, AVSpeechSynthesizerDelegate {
    private let synth = AVSpeechSynthesizer()
    private var last = ""
    private var pending: String?
    var onError: ((String) -> Void)?

    override init() {
        super.init()
        synth.delegate = self
    }

    func stop() {
        last = ""
        pending = nil
        synth.stopSpeaking(at: .immediate)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    func say(_ text: String, force: Bool = false) {
        let line = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if line.isEmpty { return }
        if !force && line == last {
            pending = nil
            return
        }
        if synth.isSpeaking && !force {
            pending = line
            return
        }
        pending = nil
        do {
            let audio = AVAudioSession.sharedInstance()
            try audio.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
            try audio.setActive(true)
        } catch {
            onError?("Voice could not start. Tap Replay to try again.")
            return
        }
        last = line
        synth.stopSpeaking(at: .immediate)
        let utterance = AVSpeechUtterance(string: line)
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate * 0.92
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        utterance.volume = 1
        synth.speak(utterance)
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        if let next = pending {
            pending = nil
            say(next)
        } else {
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
    }
}
