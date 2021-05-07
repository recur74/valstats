# valstats
Plots a users Valorant matches, comparing the players Ranked Rating with an estimated MMR.

```
Usage: valstats.py [OPTIONS] USERNAME PASSWORD

Options:
  --zone TEXT           Valorant zone (eu, na etc)
  --plot / --no-plot    Plot the result
  --print / --no-print  Print the games to terminal
  --db-name TEXT        Database name and path. Default is ./{username}.db
  --help                Show this message and exit.
```
![plot](https://user-images.githubusercontent.com/36073835/116444971-a0e54a80-a855-11eb-9ced-a49df0e65ea2.png)

MMR is calculated as the average rank of all other players in the match.
