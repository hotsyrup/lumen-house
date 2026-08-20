const starField = document.querySelector('#stars');

if (starField) {
  const fragment = document.createDocumentFragment();

  for (let index = 0; index < 90; index += 1) {
    const star = document.createElement('span');
    star.className = 'star';
    star.style.left = `${(index * 47 + 13) % 100}%`;
    star.style.top = `${(index * 71 + 7) % 78}%`;
    star.style.setProperty('--opacity', (0.22 + ((index * 17) % 65) / 100).toFixed(2));
    star.style.setProperty('--duration', `${2.2 + ((index * 19) % 28) / 10}s`);
    star.style.setProperty('--delay', `${-((index * 13) % 40) / 10}s`);
    fragment.appendChild(star);
  }

  starField.appendChild(fragment);
}

const year = document.querySelector('#year');
if (year) year.textContent = String(new Date().getFullYear());
