import { Component, signal } from '@angular/core';
import { Game } from "./components/game/game";
@Component({
  selector: 'app-root',
  imports: [Game],
  templateUrl: './app.html',
  styleUrl: './app.scss'
})
export class App {
  protected readonly title = signal('frontend');
}
